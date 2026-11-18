"""Durable reservations. Resume never resets request counts or wall deadlines."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from ..knowledge.models import digest
from .budget_ledger import BudgetExceeded, BudgetLedger, Reservation, TokenRates, WorkflowBudget
from .state import WorkflowError


@dataclass(frozen=True)
class DurableReservation(Reservation):
    ledger_id: str


class DurableBudgetLedger(BudgetLedger):
    def __init__(self, path: str | Path, ledger_id: str, budget: WorkflowBudget | None = None, *, clock=time.time,
                 cancellation_token=None, require_existing: bool = False):
        self.path, self.ledger_id, self._clock = Path(path), ledger_id, clock
        self._reservation_scope = digest([str(self.path.resolve()), ledger_id])
        self.cancellation_token = cancellation_token
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
                raise WorkflowError("incompatible_store", "durable budget schema is incompatible")
            db.execute("CREATE TABLE IF NOT EXISTS ledgers (id TEXT PRIMARY KEY,budget TEXT NOT NULL,deadline REAL NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS reservations (
                ledger TEXT NOT NULL,number INTEGER NOT NULL,prompt_ceiling INTEGER NOT NULL,completion_ceiling INTEGER NOT NULL,
                cost_ceiling REAL NOT NULL,rates TEXT,status TEXT NOT NULL,prompt INTEGER,completion INTEGER,cost REAL,
                usage_known INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(ledger,number),FOREIGN KEY(ledger) REFERENCES ledgers(id))""")
            db.execute("PRAGMA user_version=1")
            row = db.execute("SELECT budget,deadline FROM ledgers WHERE id=?", (ledger_id,)).fetchone()
            if row:
                self.budget = WorkflowBudget(**json.loads(row[0]))
                if budget is not None and self.budget != budget:
                    raise WorkflowError("input_mismatch", "workflow budget cannot change during resume")
                self._deadline = row[1]
            else:
                if require_existing:
                    raise WorkflowError("needs_action", "persisted workflow budget is missing; refusing to reset its limits")
                self.budget = budget or WorkflowBudget()
                self._deadline = clock() + self.budget.deadline_seconds
                db.execute("INSERT INTO ledgers VALUES (?,?,?)", (ledger_id, json.dumps(asdict(self.budget)), self._deadline))

    @contextmanager
    def _transaction(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error:
            if db:
                db.rollback()
            raise WorkflowError("storage_unavailable", "workflow budget transaction failed") from None
        finally:
            if db:
                db.close()

    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def _rows(self, db):
        return db.execute("SELECT * FROM reservations WHERE ledger=? ORDER BY number", (self.ledger_id,)).fetchall()

    def _check_rows(self, rows) -> None:
        if self.cancellation_token and self.cancellation_token.is_cancelled:
            raise BudgetExceeded("workflow cancelled")
        if self.remaining_seconds() <= 0:
            raise BudgetExceeded("workflow deadline exhausted")
        if sum((row["prompt"] or 0) + (row["completion"] or 0) for row in rows) > self.budget.max_tokens:
            raise BudgetExceeded("reported usage exceeded workflow token limit")
        if self.budget.max_cost_usd is not None and sum(row["cost"] or 0.0 for row in rows) > self.budget.max_cost_usd:
            raise BudgetExceeded("reported usage exceeded workflow cost limit")

    def check(self) -> None:
        with self._transaction() as db:
            self._check_rows(self._rows(db))

    def reserve(self, prompt_tokens: int, completion_tokens: int, rates: TokenRates | None = None) -> DurableReservation:
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (prompt_tokens, completion_tokens)):
            raise ValueError("token reservations must be nonnegative integers")
        with self._transaction() as db:
            rows = self._rows(db)
            self._check_rows(rows)
            if len(rows) >= self.budget.max_requests:
                raise BudgetExceeded("workflow request limit exhausted")
            used_tokens = sum((row["prompt"] or 0) + (row["completion"] or 0) if row["status"] == "settled"
                              else row["prompt_ceiling"] + row["completion_ceiling"] for row in rows)
            if used_tokens + prompt_tokens + completion_tokens > self.budget.max_tokens:
                raise BudgetExceeded("workflow token limit exhausted")
            if self.budget.max_cost_usd is not None and rates is None:
                raise BudgetExceeded("a cost limit requires trusted token rates")
            ceiling = rates.cost(prompt_tokens, completion_tokens) if rates else 0.0
            used_cost = sum((row["cost"] or 0.0) if row["status"] == "settled" else row["cost_ceiling"] for row in rows)
            if self.budget.max_cost_usd is not None and used_cost + ceiling > self.budget.max_cost_usd:
                raise BudgetExceeded("workflow cost limit exhausted")
            number = len(rows) + 1
            db.execute("INSERT INTO reservations (ledger,number,prompt_ceiling,completion_ceiling,cost_ceiling,rates,status) "
                       "VALUES (?,?,?,?,?,?,'reserved')", (self.ledger_id, number, prompt_tokens, completion_tokens, ceiling,
                                                          json.dumps(asdict(rates), sort_keys=True) if rates else None))
            return DurableReservation(number, prompt_tokens, completion_tokens, ceiling, self._reservation_scope)

    def settle(self, reservation: DurableReservation, *, prompt_tokens: int | None = None, completion_tokens: int | None = None,
               rates: TokenRates | None = None) -> None:
        if not isinstance(reservation, DurableReservation) or reservation.ledger_id != self._reservation_scope:
            raise ValueError("reservation belongs to a different durable ledger")
        known = all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in (prompt_tokens, completion_tokens))
        prompt = prompt_tokens if known else reservation.prompt_tokens
        completion = completion_tokens if known else reservation.completion_tokens
        cost = rates.cost(prompt, completion) if known and rates else reservation.cost_ceiling
        if not math.isfinite(cost):
            raise ValueError("reported cost must be finite")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM reservations WHERE ledger=? AND number=?",
                             (self.ledger_id, reservation.request_number)).fetchone()
            if (not row or (row["prompt_ceiling"], row["completion_ceiling"], row["cost_ceiling"]) !=
                    (reservation.prompt_tokens, reservation.completion_tokens, reservation.cost_ceiling)
                    or row["rates"] != (json.dumps(asdict(rates), sort_keys=True) if rates else None)):
                raise ValueError("reservation or pricing does not match its durable record")
            if row["status"] == "settled":
                if (row["prompt"], row["completion"], row["cost"], row["usage_known"]) != (prompt, completion, cost, int(known)):
                    raise ValueError("settlement was already recorded with different usage")
                return
            db.execute("UPDATE reservations SET status='settled',prompt=?,completion=?,cost=?,usage_known=? WHERE ledger=? AND number=?",
                       (prompt, completion, cost, int(known), self.ledger_id, reservation.request_number))

    def recover_pending(self) -> int:
        """Only the exclusive workflow owner may invoke this after a crash.

        A lost response is conservatively charged at the reserved ceilings.
        This method must not run while another live owner is sending requests.
        """
        with self._transaction() as db:
            return db.execute("UPDATE reservations SET status='settled',prompt=prompt_ceiling,completion=completion_ceiling,"
                              "cost=cost_ceiling,usage_known=0 WHERE ledger=? AND status='reserved'", (self.ledger_id,)).rowcount

    def snapshot(self) -> dict:
        with self._transaction() as db:
            rows = self._rows(db)
        settled = [row for row in rows if row["status"] == "settled"]
        known_cost = all(row["usage_known"] and row["rates"] is not None for row in settled)
        return {"requests": len(rows), "tokens": sum(row["prompt"] + row["completion"] for row in settled),
                "pending_requests": len(rows) - len(settled), "unknown_usage_requests": sum(not row["usage_known"] for row in settled),
                "cost_usd": sum(row["cost"] for row in settled) if known_cost else None, "cost_unavailable": not known_cost}
