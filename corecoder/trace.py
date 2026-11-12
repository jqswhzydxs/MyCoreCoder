"""Optional structured tracing for Agent runs.

Tracing is deliberately metadata-only: Agent never sends prompts, reasoning,
tool arguments, tool results, or file contents to a sink. The sinks also apply
defensive recursive redaction before retaining or writing an event.
"""

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_VERSION = 1
EVENT_TYPES = frozenset({
    "run_started",
    "run_finished",
    "model_started",
    "model_finished",
    "model_failed",
    "tool_planned",
    "schedule_decided",
    "tool_queue_finished",
    "tool_execution_finished",
    "tool_started",
    "tool_finished",
    "tool_failed",
    "cache_hit",
    "permission_allowed",
    "permission_denied",
    "context_compressed",
    "state_transition",
    "budget_checked",
    "budget_exhausted",
    "run_cancelled",
    "no_progress",
    "exploration_reminder",
    "steering_applied",
    "verification_started",
    "verification_finished",
    "verification_failed",
})
_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "file_content",
    "password",
    "passwd",
    "reasoning_content",
    "refresh_token",
    "secret",
    "system_prompt",
}
_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalise_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_").replace(" ", "_")


def _is_sensitive_key(key: Any) -> bool:
    normalised = _normalise_key(key)
    return normalised in _SENSITIVE_KEYS or normalised.endswith(("_password", "_secret", "_api_key"))


def redact(value: Any) -> Any:
    """Return a recursively redacted, JSON-safe copy of ``value``."""
    if isinstance(value, dict):
        return {
            str(key): _REDACTED if _is_sensitive_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


@dataclass(slots=True)
class TraceEvent:
    trace_id: str
    run_id: str
    step_id: int
    event_type: str
    started_at: str
    tool_call_id: str | None = None
    duration_ms: float | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_cost: float | None = None
    tool_name: str | None = None
    outcome: str | None = None
    error_category: str | None = None
    round: int | None = None
    state_version: int = STATE_VERSION
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"unknown trace event type: {self.event_type}")
        return redact(asdict(self))


class NullTraceSink:
    """Default sink: zero I/O and no retained events."""

    enabled = False

    def emit(self, event: TraceEvent) -> None:
        return None

    def status(self) -> str:
        return "disabled"


class MemoryTraceSink:
    """Thread-safe, redacted in-memory events for tests and embeddings."""

    enabled = True

    def __init__(self):
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def emit(self, event: TraceEvent) -> None:
        payload = event.to_dict()
        with self._lock:
            self.events.append(payload)

    def status(self) -> str:
        return f"enabled (memory, {len(self.events)} events)"


class JsonlTraceSink:
    """Append each redacted event with one ``O_APPEND`` write.

    A shared per-path lock orders threads and sink instances in this process;
    ``O_APPEND`` plus a single ``os.write`` keeps each JSON line indivisible at
    the file-descriptor boundary. Durability is left to the operating system so
    tracing does not put an ``fsync`` on the Agent's hot path.
    """

    enabled = True

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        key = os.path.normcase(str(self.path))
        with _PATH_LOCKS_GUARD:
            self._lock = _PATH_LOCKS.setdefault(key, threading.Lock())

    def emit(self, event: TraceEvent) -> None:
        data = (
            json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            descriptor = os.open(self.path, flags, 0o600)
            try:
                written = os.write(descriptor, data)
                if written != len(data):
                    raise OSError(f"short trace write: {written}/{len(data)} bytes")
            finally:
                os.close(descriptor)

    def status(self) -> str:
        return f"enabled (JSONL: {self.path})"


@dataclass(frozen=True, slots=True)
class TraceTimer:
    started_at: str
    monotonic_started: float


class TraceRun:
    """One ``Agent.chat`` call sharing trace/run ids and ordered step ids."""

    def __init__(
        self,
        sink,
        trace_id: str,
        model: str | None,
        *,
        run_id: str | None = None,
    ):
        self.sink = sink
        self.enabled = getattr(sink, "enabled", True)
        self.trace_id = trace_id
        self.run_id = run_id or uuid.uuid4().hex
        self.model = model
        self._step = 0
        self._lock = threading.Lock()

    @staticmethod
    def timer() -> TraceTimer:
        return TraceTimer(started_at=_utc_now(), monotonic_started=time.perf_counter())

    def emit(
        self,
        event_type: str,
        *,
        timer: TraceTimer | None = None,
        finished: bool = False,
        tool_call_id: str | None = None,
        model: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        estimated_cost: float | None = None,
        tool_name: str | None = None,
        outcome: str | None = None,
        error_category: str | None = None,
        round: int | None = None,
        state_version: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        started_at = timer.started_at if timer else _utc_now()
        duration_ms = None
        if timer is not None and finished:
            duration_ms = round_ms((time.perf_counter() - timer.monotonic_started) * 1000)
        with self._lock:
            self._step += 1
            event = TraceEvent(
                trace_id=self.trace_id,
                run_id=self.run_id,
                step_id=self._step,
                tool_call_id=tool_call_id,
                event_type=event_type,
                started_at=started_at,
                duration_ms=duration_ms,
                model=model if model is not None else self.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost=estimated_cost,
                tool_name=tool_name,
                outcome=outcome,
                error_category=error_category,
                round=round,
                state_version=STATE_VERSION if state_version is None else state_version,
                details=details or {},
            )
            try:
                self.sink.emit(event)
            except Exception:  # noqa: BLE001 - observability must never break the task
                return


def round_ms(value: float) -> float:
    return round(max(0.0, value), 3)


class TraceRecorder:
    """Factory for fail-open run recorders sharing one Agent-level trace id."""

    def __init__(self, sink=None):
        self.sink = sink if sink is not None else NullTraceSink()
        self.trace_id = uuid.uuid4().hex

    def new_run(self, model: str | None = None, *, run_id: str | None = None) -> TraceRun:
        return TraceRun(self.sink, self.trace_id, model, run_id=run_id)

    def status(self) -> str:
        try:
            return self.sink.status()
        except Exception:  # noqa: BLE001
            return "enabled (status unavailable)"
