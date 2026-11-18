"""One request/token/deadline ledger shared by all model operations.

Token estimates are conservative UTF-8 byte counts, not tokenizer measurements.
Unknown usage is charged at the reserved ceiling and remains explicitly unknown.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass


class BudgetExceeded(RuntimeError):
    """No further requests may start under the current workflow budget."""


@dataclass(frozen=True)
class WorkflowBudget:
    max_requests: int = 12
    max_tokens: int = 100_000
    deadline_seconds: float = 120.0
    max_cost_usd: float | None = None

    def __post_init__(self) -> None:
        for value in (self.max_requests, self.max_tokens):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("request and token limits must be positive integers")
        if not math.isfinite(self.deadline_seconds) or self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be finite and positive")
        if self.max_cost_usd is not None and (not math.isfinite(self.max_cost_usd) or self.max_cost_usd <= 0):
            raise ValueError("max_cost_usd must be finite and positive")


@dataclass(frozen=True)
class TokenRates:
    """Caller-supplied trusted USD rates per million tokens."""

    input_per_million: float
    output_per_million: float

    def __post_init__(self) -> None:
        if any(not math.isfinite(value) or value < 0 for value in (self.input_per_million, self.output_per_million)):
            raise ValueError("token rates must be finite and nonnegative")

    def cost(self, prompt: int, completion: int) -> float:
        return (prompt * self.input_per_million + completion * self.output_per_million) / 1_000_000


@dataclass(frozen=True)
class Reservation:
    request_number: int
    prompt_tokens: int
    completion_tokens: int
    cost_ceiling: float


class BudgetLedger:
    """Thread-safe reservations prevent simultaneous calls overspending a budget."""

    def __init__(self, budget: WorkflowBudget | None = None, *, clock=time.monotonic):
        self.budget = budget or WorkflowBudget()
        self._clock = clock
        self._deadline = clock() + self.budget.deadline_seconds
        self._lock = threading.Lock()
        self._requests = 0
        self._tokens = 0
        self._charged_cost = 0.0
        self._cost_known = True
        self._unknown_usage_requests = 0
        self._reservations: dict[int, Reservation] = {}

    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def check(self) -> None:
        if self.remaining_seconds() <= 0:
            raise BudgetExceeded("workflow deadline exhausted")
        if self._tokens > self.budget.max_tokens:
            raise BudgetExceeded("reported usage exceeded workflow token limit")
        if self.budget.max_cost_usd is not None and self._charged_cost > self.budget.max_cost_usd:
            raise BudgetExceeded("reported usage exceeded workflow cost limit")

    def reserve(self, prompt_tokens: int, completion_tokens: int, rates: TokenRates | None = None) -> Reservation:
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (prompt_tokens, completion_tokens)):
            raise ValueError("token reservations must be nonnegative integers")
        with self._lock:
            self.check()
            if self._requests >= self.budget.max_requests:
                raise BudgetExceeded("workflow request limit exhausted")
            reserved_tokens = sum(r.prompt_tokens + r.completion_tokens for r in self._reservations.values())
            if self._tokens + reserved_tokens + prompt_tokens + completion_tokens > self.budget.max_tokens:
                raise BudgetExceeded("workflow token limit exhausted")
            if self.budget.max_cost_usd is not None and rates is None:
                raise BudgetExceeded("a cost limit requires trusted token rates")
            ceiling = rates.cost(prompt_tokens, completion_tokens) if rates else 0.0
            reserved_cost = sum(r.cost_ceiling for r in self._reservations.values())
            if self.budget.max_cost_usd is not None and self._charged_cost + reserved_cost + ceiling > self.budget.max_cost_usd:
                raise BudgetExceeded("workflow cost limit exhausted")
            self._requests += 1
            reservation = Reservation(self._requests, prompt_tokens, completion_tokens, ceiling)
            self._reservations[reservation.request_number] = reservation
            return reservation

    def settle(
        self,
        reservation: Reservation,
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        rates: TokenRates | None = None,
    ) -> None:
        with self._lock:
            if self._reservations.get(reservation.request_number) is not reservation:
                raise ValueError("reservation has already been settled or belongs to another ledger")
            known = all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (prompt_tokens, completion_tokens))
            prompt = prompt_tokens if known else reservation.prompt_tokens
            completion = completion_tokens if known else reservation.completion_tokens
            self._tokens += prompt + completion
            self._charged_cost += rates.cost(prompt, completion) if known and rates else reservation.cost_ceiling
            self._cost_known = self._cost_known and known and rates is not None
            self._unknown_usage_requests += int(not known)
            del self._reservations[reservation.request_number]

    def snapshot(self) -> dict:
        """Content-free counters suitable for reports; unknown cost is never zero."""
        with self._lock:
            return {
                "requests": self._requests,
                "tokens": self._tokens,
                "pending_requests": len(self._reservations),
                "unknown_usage_requests": self._unknown_usage_requests,
                "cost_usd": self._charged_cost if self._cost_known else None,
                "cost_unavailable": not self._cost_known,
            }
