"""Typed, fail-open UI events for observing one Agent run.

These events are intentionally separate from :mod:`corecoder.trace`.  UI
events may contain transient assistant text, while Trace remains a durable,
metadata-only diagnostic channel.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = frozenset({
    "access_token",
    "api_key",
    "arguments",
    "authorization",
    "client_secret",
    "command",
    "content",
    "file_content",
    "new_string",
    "old_string",
    "password",
    "passwd",
    "reasoning",
    "reasoning_content",
    "refresh_token",
    "secret",
    "system_prompt",
    "tool_arguments",
    "write_content",
})
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|rk|pk)-[a-z0-9_-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/-]{8,}"),
    re.compile(
        r"(?i)((?:access[_ -]?token|api[_ -]?key|authorization|password|passwd|secret)"
        r"\s*[:=]\s*)[^\s,;]+"
    ),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _normalise_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_").replace(" ", "_")


def _is_sensitive_key(key: Any) -> bool:
    normalised = _normalise_key(key)
    return normalised in _SENSITIVE_KEYS or normalised.endswith(
        ("_password", "_secret", "_api_key", "_token")
    )


def _redact_string(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda match: (
                f"{match.group(1)}{_REDACTED}" if match.lastindex else _REDACTED
            ),
            result,
        )
    return result


def redact_public_data(value: Any) -> Any:
    """Return a recursively redacted, JSON-safe copy for ``public_data``.

    The policy fails closed for unknown objects and content-bearing keys.  An
    event producer that needs to expose transient assistant text must use the
    event's ``message`` field instead of smuggling it through public metadata.
    """
    if isinstance(value, Mapping):
        return {
            str(key): (
                _REDACTED
                if _is_sensitive_key(key)
                else redact_public_data(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_public_data(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _redact_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__module__}.{type(value).__qualname__}>"


class AgentEventType(str, Enum):
    RUN_STARTED = "run_started"
    PHASE_CHANGED = "phase_changed"
    MODEL_STARTED = "model_started"
    MODEL_STREAM_ACTIVITY = "model_stream_activity"
    ASSISTANT_TOKEN = "assistant_token"
    MODEL_FINISHED = "model_finished"
    MODEL_FAILED = "model_failed"
    TOOL_PLANNED = "tool_planned"
    TOOL_STARTED = "tool_started"
    TOOL_OUTPUT = "tool_output"
    CACHE_HIT = "cache_hit"
    TOOL_FINISHED = "tool_finished"
    TOOL_FAILED = "tool_failed"
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_FINISHED = "verification_finished"
    STEERING_RECEIVED = "steering_received"
    STEERING_APPLIED = "steering_applied"
    REPLANNING = "replanning"
    PAUSE_REQUESTED = "pause_requested"
    RUN_PAUSED = "run_paused"
    RUN_RESUMED = "run_resumed"
    CANCELLATION_REQUESTED = "cancellation_requested"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    event_id: str
    sequence: int
    run_id: str
    event_type: AgentEventType
    timestamp: str
    phase: str
    message: str = ""
    public_data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        event_type = self.event_type
        if not isinstance(event_type, AgentEventType):
            event_type = AgentEventType(event_type)
            object.__setattr__(self, "event_type", event_type)
        safe = redact_public_data(self.public_data)
        object.__setattr__(self, "public_data", safe if isinstance(safe, dict) else {})

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["event_type"] = self.event_type.value
        return payload


class EventSink(Protocol):
    def emit(self, event: AgentEvent) -> None: ...


class NullEventSink:
    """Default sink: retain nothing and perform no I/O."""

    def emit(self, event: AgentEvent) -> None:
        return None


class CallbackEventSink:
    """Adapt a simple ``Callable[[AgentEvent], None]`` to an event sink."""

    def __init__(self, callback: Callable[[AgentEvent], None]):
        if not callable(callback):
            raise TypeError("event callback must be callable")
        self.callback = callback

    def emit(self, event: AgentEvent) -> None:
        self.callback(event)


class QueueEventSink:
    """In-process queue sink for pull-based integrations.

    The default queue is unbounded.  A bounded full queue drops the newest
    observation rather than blocking the Agent's execution path.
    """

    def __init__(self, maxsize: int = 0):
        self._queue: queue.Queue[AgentEvent] = queue.Queue(maxsize=max(0, maxsize))

    def emit(self, event: AgentEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            return

    def get(self, timeout: float | None = None) -> AgentEvent:
        if timeout is None:
            return self._queue.get()
        return self._queue.get(timeout=timeout)

    def get_nowait(self) -> AgentEvent:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()

    def drain(self) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                return events


class CompositeEventSink:
    """Fan out to sinks independently; one broken observer cannot stop others."""

    def __init__(self, *sinks: EventSink | Iterable[EventSink]):
        if len(sinks) == 1 and not hasattr(sinks[0], "emit"):
            self.sinks = tuple(sinks[0])
        else:
            self.sinks = tuple(sinks)

    def emit(self, event: AgentEvent) -> None:
        for sink in self.sinks:
            try:
                sink.emit(event)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001, S112 - observers must fail open
                continue


class EventEmitter:
    """Create ordered events for one run and deliver them fail-open."""

    def __init__(self, run_id: str, sink: EventSink | None = None):
        self.run_id = str(run_id)
        self.sink = sink if sink is not None else NullEventSink()
        self._sequence = 0
        self._lock = threading.RLock()
        self._observers: list[Callable[[AgentEvent], None]] = []

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def add_observer(self, observer: Callable[[AgentEvent], None]) -> None:
        """Register an internal, fail-open observer (metrics, diagnostics)."""
        with self._lock:
            self._observers.append(observer)

    @contextmanager
    def synchronized(self):
        """Serialize a control-plane mutation with event sequence allocation.

        Runtime steering uses this narrow transaction so an accepted control
        cannot race behind the run's terminal event.  The re-entrant lock also
        permits an event callback to enqueue steering without deadlocking.
        """
        with self._lock:
            yield

    def emit(
        self,
        event_type: AgentEventType | str,
        *,
        phase: str,
        message: str = "",
        public_data: Mapping[str, Any] | None = None,
    ) -> AgentEvent:
        """Allocate and deliver one event without exposing sink failures."""
        with self._lock:
            self._sequence += 1
            event = AgentEvent(
                event_id=uuid.uuid4().hex,
                sequence=self._sequence,
                run_id=self.run_id,
                event_type=AgentEventType(event_type),
                timestamp=_utc_now(),
                phase=str(phase),
                message=str(message),
                public_data=dict(public_data or {}),
            )
            # Observe before handing the event to a potentially slow external
            # sink: runtime metrics must measure the run, not consumer backlog.
            for observer in tuple(self._observers):
                try:
                    observer(event)
                except Exception:  # noqa: BLE001, S112 - observers must fail open
                    continue
            try:
                # Delivery remains under the sequence lock so concurrent tool
                # workers cannot enqueue sequence N+1 before sequence N.
                self.sink.emit(event)
            except Exception:  # noqa: BLE001, S110 - observability must fail open
                pass
            return event


def safe_tool_summary(tool_name: str, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return content-free metadata for tool lifecycle events."""
    arguments = arguments or {}
    return {
        "tool_name": str(tool_name),
        "argument_count": len(arguments),
        "argument_names": sorted(str(key) for key in arguments),
    }


def build_event_sink(
    *,
    on_event: Callable[[AgentEvent], None] | None = None,
    event_sink: EventSink | None = None,
) -> EventSink:
    """Compose the two public Agent integration forms into one sink."""
    sinks: list[EventSink] = []
    if event_sink is not None:
        if not callable(getattr(event_sink, "emit", None)):
            raise TypeError("event_sink must provide emit(event)")
        sinks.append(event_sink)
    if on_event is not None:
        sinks.append(CallbackEventSink(on_event))
    if not sinks:
        return NullEventSink()
    if len(sinks) == 1:
        return sinks[0]
    return CompositeEventSink(*sinks)


def json_text(event: AgentEvent) -> str:
    """Serialize an already-projected event for embeddings and tests."""
    return json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)


__all__ = [
    "AgentEvent",
    "AgentEventType",
    "CallbackEventSink",
    "CompositeEventSink",
    "EventEmitter",
    "EventSink",
    "NullEventSink",
    "QueueEventSink",
    "build_event_sink",
    "json_text",
    "redact_public_data",
    "safe_tool_summary",
]
