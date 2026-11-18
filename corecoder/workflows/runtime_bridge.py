"""Persist child identity before entering the existing Agent execution loop."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..knowledge.models import ProjectScope, digest
from .claims import exclusive_file_claim
from .state import WorkflowError


class RuntimeBridge:
    def __init__(self, scope: ProjectScope, path: str | Path, *, fault=None):
        self.scope, self.path, self.fault = scope, Path(path), fault
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
                raise WorkflowError("incompatible_store", "runtime operation schema is incompatible")
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,owner TEXT NOT NULL,project TEXT NOT NULL,root_hash TEXT NOT NULL,
                thread_id TEXT NOT NULL,turn_id TEXT NOT NULL,plan_hash TEXT NOT NULL,attempt INTEGER NOT NULL,
                child_run_id TEXT UNIQUE NOT NULL,input_hash TEXT NOT NULL)""")
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error:
            if db:
                db.rollback()
            raise WorkflowError("storage_unavailable", "runtime operation transaction failed") from None
        finally:
            if db:
                db.close()

    def execute(self, agent, task_spec, *, thread_id: str, turn_id: str, plan_hash: str, attempt: int = 0,
                resume_paused: bool = False, cancellation_token=None, on_event=None, budget=None):
        if attempt != 0 or isinstance(attempt, bool):
            raise WorkflowError("configuration_error", "automatic whole-task retries are disabled")
        if not thread_id or not turn_id or len(thread_id) > 200 or len(turn_id) > 200:
            raise ValueError("thread and turn identities are required")
        if len(plan_hash) != 64 or any(character not in "0123456789abcdef" for character in plan_hash):
            raise ValueError("plan hash must be a trusted SHA-256 digest")
        if agent.run_store is None:
            raise WorkflowError("configuration_error", "RuntimeBridge requires an Agent with a durable RunStore")
        goal, criteria, _ = agent._normalise_task_spec(task_spec)
        operation = digest([self.scope.identity(), thread_id, turn_id, plan_hash, attempt])
        child = digest(["child-run-v1", operation])
        input_hash = digest([goal.strip(), criteria, plan_hash, digest(str(agent.run_store.path))])
        root_hash = digest(str(self.scope.root))
        with exclusive_file_claim(self.path.parent / "operation-claims" / (operation + ".lock")):
            with self.transaction() as db:
                db.execute("INSERT OR IGNORE INTO operations VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (operation, self.scope.owner_id, self.scope.project_id, root_hash, thread_id, turn_id,
                            plan_hash, attempt, child, input_hash))
                row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation,)).fetchone()
                if row["input_hash"] != input_hash or row["root_hash"] != root_hash or row["child_run_id"] != child:
                    raise WorkflowError("input_mismatch", "runtime operation was already bound to different input or storage")
            if self.fault:
                self.fault("after_mapping", {"operation_id": operation, "child_run_id": child})
            result = agent.run_once(task_spec, run_id=child, configuration_hash=plan_hash, resume_paused=resume_paused,
                                    cancellation_token=cancellation_token, on_event=on_event, budget=budget)
            if self.fault:
                self.fault("after_child", {"operation_id": operation, "child_run_id": child})
            return result

    def child_id(self, *, thread_id: str, turn_id: str, plan_hash: str, attempt: int = 0) -> str | None:
        operation = digest([self.scope.identity(), thread_id, turn_id, plan_hash, attempt])
        with self.transaction() as db:
            row = db.execute("SELECT child_run_id FROM operations WHERE operation_id=? AND owner=? AND project=? AND root_hash=?",
                             (operation, self.scope.owner_id, self.scope.project_id, digest(str(self.scope.root)))).fetchone()
        return row[0] if row else None
