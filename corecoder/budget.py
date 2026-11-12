"""Run budgets, cooperative cancellation, and no-progress detection."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from .state import ExitReason, RunState
from .tools.protocol import ToolResult


@dataclass(frozen=True, slots=True)
class RunBudget:
    """Hard limits for one :meth:`Agent.run` invocation.

    ``None`` disables a limit. Zero is a useful value: it prevents the
    corresponding operation from starting at all.
    """

    max_rounds: int | None = None
    max_tool_calls: int | None = None
    max_prompt_tokens: int | None = None
    max_completion_tokens: int | None = None
    max_cost_usd: float | None = None
    deadline_seconds: float | None = None
    max_no_progress_steps: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "max_rounds",
            "max_tool_calls",
            "max_prompt_tokens",
            "max_completion_tokens",
            "max_no_progress_steps",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise TypeError(f"RunBudget.{name} must be an integer or None")
            if value is not None and value < 0:
                raise ValueError(f"RunBudget.{name} must be non-negative")
        for name in ("max_cost_usd", "deadline_seconds"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, (int, float)) or isinstance(value, bool)
            ):
                raise TypeError(f"RunBudget.{name} must be a number or None")
            if value is not None and value < 0:
                raise ValueError(f"RunBudget.{name} must be non-negative")

    def to_dict(self) -> dict[str, int | float | None]:
        return asdict(self)


class CancellationToken:
    """Thread-safe cooperative cancellation shared by callers and the CLI."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = "Cancellation requested by the caller."

    def cancel(self, reason: str | None = None) -> None:
        with self._lock:
            if reason:
                self._reason = str(reason)
            self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason


@dataclass(slots=True)
class BudgetUsage:
    rounds: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    cost_unavailable: bool = False
    elapsed_seconds: float = 0.0
    no_progress_steps: int = 0

    def to_dict(self) -> dict[str, int | float | bool | None]:
        payload = asdict(self)
        payload["elapsed_seconds"] = round(self.elapsed_seconds, 6)
        if self.cost_usd is not None:
            payload["cost_usd"] = round(self.cost_usd, 12)
        return payload


class RunStopped(RuntimeError):
    """Internal control-flow exception for an expected, safe stop."""

    def __init__(self, exit_reason: ExitReason, message: str, *, cancelled: bool = False):
        self.exit_reason = exit_reason
        self.message = message
        self.cancelled = cancelled
        super().__init__(message)


def canonicalize_arguments(arguments: Any) -> str:
    """Return one stable JSON representation independent of dict key order."""
    return json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def result_summary(result: ToolResult) -> dict[str, Any]:
    """Build a stable, content-free summary suitable for a fingerprint."""
    content = result.model_content()
    return {
        "ok": result.ok,
        "outcome": result.outcome.value,
        "error_category": result.error_category,
        "error_code": result.error_code,
        "retryable": result.retryable,
        "content_type": type(result.data).__name__ if result.ok else "safe_error",
        "content_length": len(content),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def action_fingerprint(tool_name: str, arguments: Any, result: ToolResult) -> str:
    """Hash ``tool name + canonical arguments + result summary``."""
    payload = {
        "tool_name": tool_name,
        "arguments": json.loads(canonicalize_arguments(arguments)),
        "result_summary": result_summary(result),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_default(value: Any) -> str:
    return f"<{type(value).__module__}.{type(value).__qualname__}>"


@dataclass(frozen=True, slots=True)
class _ActionObservation:
    version_delta: int
    evidence_signature: str
    progress_signature: str


@dataclass(frozen=True, slots=True)
class NoProgressObservation:
    """A model-visible, body-free hint that exploration stalled."""

    tool_name: str
    tool_call_id: str
    count: int
    action_fingerprint: str
    repeated_without_progress: bool = True


class RunBudgetController:
    """Controller-owned accounting checked at model and tool boundaries."""

    def __init__(
        self,
        budget: RunBudget,
        cancellation_token: CancellationToken,
        llm,
    ) -> None:
        self.budget = budget
        self.cancellation_token = cancellation_token
        self.llm = llm
        self.usage = BudgetUsage()
        self._started = time.monotonic()
        self._prompt_before = self._counter("total_prompt_tokens")
        self._completion_before = self._counter("total_completion_tokens")
        self._cost_before = self._cost()
        self._response_prompt_tokens = 0
        self._response_completion_tokens = 0
        self._tool_start_versions: dict[str, int] = {}
        self._observations: dict[str, _ActionObservation] = {}
        self._semantic_evidence: set[str] = set()

    def start_round(self, state: RunState) -> None:
        self.usage.rounds += 1
        self._sync_state(state)

    def record_model_response(self, response, state: RunState) -> None:
        self._response_prompt_tokens += max(0, int(getattr(response, "prompt_tokens", 0) or 0))
        self._response_completion_tokens += max(
            0,
            int(getattr(response, "completion_tokens", 0) or 0),
        )
        self._refresh(state)

    def before_tools(self, tool_calls, state: RunState, trace_run) -> None:
        self.check("before_tool", state, trace_run)
        count = len(tool_calls)
        limit = self.budget.max_tool_calls
        if limit is not None and self.usage.tool_calls + count > limit:
            self._stop(
                ExitReason.MAX_TOOL_CALLS,
                "max_tool_calls",
                limit,
                state,
                trace_run,
            )
        for call in tool_calls:
            self._tool_start_versions[call.id] = state.state_version
        self.usage.tool_calls += count
        self._sync_state(state)
        self._emit_check("before_tool", state, trace_run)

    def observe_tool(
        self,
        tool_call,
        result: ToolResult,
        state: RunState,
        trace_run,
    ) -> NoProgressObservation | None:
        fingerprint = action_fingerprint(tool_call.name, tool_call.arguments, result)
        start_version = self._tool_start_versions.pop(tool_call.id, state.state_version)
        version_delta = max(0, state.state_version - start_version)

        if result.ok:
            self._semantic_evidence.add(fingerprint)
        evidence_signature = self._evidence_signature(state)
        progress_signature = self._digest(state.task_progress)
        current = _ActionObservation(version_delta, evidence_signature, progress_signature)
        previous = self._observations.get(fingerprint)
        repeated_without_progress = (
            previous is not None
            and current.evidence_signature == previous.evidence_signature
            and current.progress_signature == previous.progress_signature
            # State version always advances for bookkeeping. Only additional
            # controller-owned mutations beyond the prior path can signal
            # progress; an equal or smaller delta is the same stalled path.
            and current.version_delta <= previous.version_delta
        )
        self._observations[fingerprint] = current

        if repeated_without_progress:
            self.usage.no_progress_steps += 1
            observation = NoProgressObservation(
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
                count=self.usage.no_progress_steps,
                action_fingerprint=fingerprint,
            )
            trace_run.emit(
                "no_progress",
                round=state.round,
                state_version=state.state_version,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                outcome="repeated_without_progress",
                details={
                    "action_fingerprint": fingerprint,
                    "state_version_delta": version_delta,
                    "no_progress_steps": self.usage.no_progress_steps,
                },
            )
        else:
            observation = None
            self.usage.no_progress_steps = 0
        self._sync_state(state)
        self.check("after_tool", state, trace_run)
        return observation

    def check(self, boundary: str, state: RunState, trace_run) -> None:
        self._refresh(state)
        self._emit_check(boundary, state, trace_run)

        if self.cancellation_token.is_cancelled:
            message = (
                f"Run cancelled safely: {self.cancellation_token.reason} "
                "No additional model or tool operation was started."
            )
            trace_run.emit(
                "run_cancelled",
                round=state.round,
                state_version=state.state_version,
                outcome="cancelled",
                error_category="user_cancelled",
                details={"boundary": boundary, "budget_usage": self.usage.to_dict()},
            )
            raise RunStopped(ExitReason.CANCELLED, message, cancelled=True)

        deadline = self.budget.deadline_seconds
        if deadline is not None and self.usage.elapsed_seconds >= deadline:
            self._stop(
                ExitReason.DEADLINE_EXCEEDED,
                "deadline_seconds",
                deadline,
                state,
                trace_run,
            )

        no_progress = self.budget.max_no_progress_steps
        if (
            no_progress is not None
            and self.usage.no_progress_steps > 0
            and self.usage.no_progress_steps >= no_progress
        ):
            message = (
                "Stopped after repeated actions made no progress. Inspect the last tool's "
                "arguments/result, change the approach, or increase "
                f"RunBudget.max_no_progress_steps (current limit: {no_progress})."
            )
            trace_run.emit(
                "budget_exhausted",
                round=state.round,
                state_version=state.state_version,
                outcome=ExitReason.NO_PROGRESS.value,
                error_category="no_progress",
                details={"boundary": boundary, "budget_usage": self.usage.to_dict()},
            )
            raise RunStopped(ExitReason.NO_PROGRESS, message)

        inclusive = boundary in {
            "before_model",
            "before_aux_model",
            "before_tool",
            "before_next_round",
        }
        post_model = boundary == "after_model"
        if boundary in {"before_model", "before_next_round"}:
            self._check_value(
                "max_rounds",
                self.usage.rounds,
                ExitReason.MAX_ROUNDS,
                state,
                trace_run,
                inclusive=True,
            )
        if inclusive or post_model:
            self._check_value(
                "max_prompt_tokens",
                self.usage.prompt_tokens,
                ExitReason.MAX_PROMPT_TOKENS,
                state,
                trace_run,
                inclusive=inclusive,
            )
            self._check_value(
                "max_completion_tokens",
                self.usage.completion_tokens,
                ExitReason.MAX_COMPLETION_TOKENS,
                state,
                trace_run,
                inclusive=inclusive,
            )
            if not self.usage.cost_unavailable:
                self._check_value(
                    "max_cost_usd",
                    self.usage.cost_usd or 0.0,
                    ExitReason.MAX_COST_USD,
                    state,
                    trace_run,
                    inclusive=inclusive,
                )

    def _check_value(
        self,
        field: str,
        used: float,
        reason: ExitReason,
        state: RunState,
        trace_run,
        *,
        inclusive: bool,
    ) -> None:
        limit = getattr(self.budget, field)
        if limit is None:
            return
        exhausted = used >= limit if inclusive else used > limit
        if exhausted:
            self._stop(reason, field, limit, state, trace_run)

    def _stop(
        self,
        reason: ExitReason,
        field: str,
        limit: float,
        state: RunState,
        trace_run,
    ) -> None:
        used = {
            "max_rounds": self.usage.rounds,
            "max_tool_calls": self.usage.tool_calls,
            "max_prompt_tokens": self.usage.prompt_tokens,
            "max_completion_tokens": self.usage.completion_tokens,
            "max_cost_usd": self.usage.cost_usd,
            "deadline_seconds": self.usage.elapsed_seconds,
        }.get(field)
        message = (
            f"Run stopped because RunBudget.{field} was exhausted "
            f"(limit={limit}, used={used}). Narrow the task or increase this limit, then retry."
        )
        trace_run.emit(
            "budget_exhausted",
            round=state.round,
            state_version=state.state_version,
            outcome=reason.value,
            error_category=field,
            details={
                "limit_name": field,
                "limit": limit,
                "used": used,
                "budget_usage": self.usage.to_dict(),
            },
        )
        raise RunStopped(reason, message)

    def _refresh(self, state: RunState) -> None:
        self.usage.elapsed_seconds = max(0.0, time.monotonic() - self._started)
        prompt_delta = self._counter_delta("total_prompt_tokens", self._prompt_before)
        completion_delta = self._counter_delta(
            "total_completion_tokens",
            self._completion_before,
        )
        self.usage.prompt_tokens = max(self._response_prompt_tokens, prompt_delta or 0)
        self.usage.completion_tokens = max(
            self._response_completion_tokens,
            completion_delta or 0,
        )
        cost = self._cost()
        if cost is None or self._cost_before is None:
            self.usage.cost_usd = None
            self.usage.cost_unavailable = True
        else:
            self.usage.cost_usd = max(0.0, cost - self._cost_before)
            self.usage.cost_unavailable = False
        self._sync_state(state)

    def _sync_state(self, state: RunState) -> None:
        state.budget_usage = self.usage.to_dict()

    def _emit_check(self, boundary: str, state: RunState, trace_run) -> None:
        trace_run.emit(
            "budget_checked",
            round=state.round,
            state_version=state.state_version,
            outcome="within_budget",
            details={
                "boundary": boundary,
                "budget": self.budget.to_dict(),
                "budget_usage": self.usage.to_dict(),
                "cost_unavailable": self.usage.cost_unavailable,
            },
        )

    def _counter(self, name: str) -> int | None:
        try:
            value = getattr(self.llm, name, None)
        except Exception:  # noqa: BLE001 - accounting is best effort
            return None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _counter_delta(self, name: str, before: int | None) -> int | None:
        if before is None:
            return None
        after = self._counter(name)
        return None if after is None else max(0, after - before)

    def _cost(self) -> float | None:
        try:
            value = getattr(self.llm, "estimated_cost", None)
        except Exception:  # noqa: BLE001 - accounting is best effort
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None

    def _evidence_signature(self, state: RunState) -> str:
        normalised = []
        for ref in state.evidence_refs:
            parts = ref.split(":")
            normalised.append(f"tool:{parts[-1]}" if len(parts) >= 3 and parts[0] == "tool" else ref)
        normalised.extend(self._semantic_evidence)
        return self._digest(sorted(set(normalised)))

    @staticmethod
    def _digest(value: Any) -> str:
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
