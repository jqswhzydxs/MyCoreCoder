"""Content-free wall-clock metrics for one agent run.

These metrics are deliberately separate from evaluation latency simulation:
they measure real monotonic time spent in the local runtime.  Only event
timestamps, durations and counters are retained; prompts, tool arguments,
commands, file contents and results never enter this module.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from corecoder.events import AgentEvent, AgentEventType

_TERMINAL_EVENTS = {
    AgentEventType.RUN_FINISHED,
    AgentEventType.RUN_FAILED,
    AgentEventType.RUN_CANCELLED,
}


def _milliseconds(seconds: float) -> float:
    return round(max(0.0, seconds) * 1000.0, 3)


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


class RunMetrics:
    """Accumulate latency metrics from an :class:`EventEmitter` stream."""

    def __init__(self, *, started_at: float | None = None) -> None:
        self._started = time.monotonic() if started_at is None else started_at
        self._lock = threading.Lock()
        self._first_event_at: float | None = None
        self._first_model_activity_at: float | None = None
        self._first_tool_output_at: float | None = None
        self._model_started_at: float | None = None
        self._model_total = 0.0
        self._model_calls = 0
        self._planned_at: dict[str, float] = {}
        self._started_at_by_call: dict[str, float] = {}
        self._queue_total = 0.0
        self._tool_total = 0.0
        self._execution_total = 0.0
        self._tool_calls = 0
        self._tool_executions = 0
        self._tool_outputs = 0
        self._cache_hits = 0
        self._cache_hit_ids: set[str] = set()
        self._steering_received_at: dict[str, float] = {}
        self._steering_ack: list[float] = []
        self._steering_apply: list[float] = []
        self._finished_at: float | None = None

    def observe(self, event: AgentEvent) -> None:
        """Consume one event; never raises into the event delivery path."""
        try:
            self._observe(event)
        except Exception:  # noqa: BLE001 - metrics must never break a run
            # Metrics are diagnostic and must not break a run or its sinks.
            return

    def _observe(self, event: AgentEvent) -> None:
        now = time.monotonic()
        data = event.public_data if isinstance(event.public_data, dict) else {}
        call_id = str(data.get("tool_call_id") or "")
        event_type = event.event_type

        with self._lock:
            if self._first_event_at is None:
                self._first_event_at = now

            if event_type is AgentEventType.MODEL_STARTED:
                self._model_started_at = now
                self._model_calls += 1
            elif event_type in {
                AgentEventType.MODEL_STREAM_ACTIVITY,
                AgentEventType.ASSISTANT_TOKEN,
            }:
                if self._first_model_activity_at is None:
                    self._first_model_activity_at = now
            elif event_type in {
                AgentEventType.MODEL_FINISHED,
                AgentEventType.MODEL_FAILED,
            }:
                if self._first_model_activity_at is None:
                    self._first_model_activity_at = now
                if self._model_started_at is not None:
                    self._model_total += max(0.0, now - self._model_started_at)
                    self._model_started_at = None

            if event_type is AgentEventType.TOOL_PLANNED and call_id:
                self._planned_at[call_id] = now
            elif event_type is AgentEventType.TOOL_STARTED and call_id:
                self._tool_calls += 1
                planned = self._planned_at.pop(call_id, None)
                if planned is not None:
                    self._queue_total += max(0.0, now - planned)
                self._started_at_by_call[call_id] = now
            elif event_type is AgentEventType.TOOL_OUTPUT:
                self._tool_outputs += 1
                if self._first_tool_output_at is None:
                    self._first_tool_output_at = now
            elif event_type is AgentEventType.CACHE_HIT:
                self._cache_hits += 1
                if call_id:
                    self._cache_hit_ids.add(call_id)
            elif event_type in {
                AgentEventType.TOOL_FINISHED,
                AgentEventType.TOOL_FAILED,
            }:
                started = self._started_at_by_call.pop(call_id, None)
                if started is not None:
                    duration = max(0.0, now - started)
                    self._tool_total += duration
                    if call_id not in self._cache_hit_ids:
                        self._execution_total += duration
                if call_id not in self._cache_hit_ids:
                    self._tool_executions += 1

            if event_type is AgentEventType.STEERING_RECEIVED:
                instruction_id = str(data.get("instruction_id") or "")
                if instruction_id:
                    self._steering_received_at[instruction_id] = now
                ack_ms = data.get("ack_ms")
                if isinstance(ack_ms, (int, float)):
                    self._steering_ack.append(float(ack_ms))
            elif event_type is AgentEventType.STEERING_APPLIED:
                instruction_id = str(data.get("instruction_id") or "")
                apply_ms = data.get("apply_ms")
                if isinstance(apply_ms, (int, float)):
                    self._steering_apply.append(float(apply_ms))
                elif instruction_id and instruction_id in self._steering_received_at:
                    self._steering_apply.append(
                        _milliseconds(now - self._steering_received_at[instruction_id])
                    )

            if event_type in _TERMINAL_EVENTS:
                self._finished_at = now

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of the run metrics."""
        with self._lock:
            finished = self._finished_at if self._finished_at is not None else time.monotonic()
            return {
                "time_to_first_event_ms": (
                    _milliseconds(self._first_event_at - self._started)
                    if self._first_event_at is not None
                    else None
                ),
                "time_to_first_model_activity_ms": (
                    _milliseconds(self._first_model_activity_at - self._started)
                    if self._first_model_activity_at is not None
                    else None
                ),
                "time_to_first_tool_output_ms": (
                    _milliseconds(self._first_tool_output_at - self._started)
                    if self._first_tool_output_at is not None
                    else None
                ),
                "model_duration_ms": round(self._model_total * 1000.0, 3),
                "tool_queue_duration_ms": round(self._queue_total * 1000.0, 3),
                "tool_duration_ms": round(self._tool_total * 1000.0, 3),
                "tool_execution_duration_ms": round(self._execution_total * 1000.0, 3),
                "steering_ack_ms": _average(self._steering_ack),
                "steering_apply_ms": _average(self._steering_apply),
                "steering_ack_count": len(self._steering_ack),
                "steering_apply_count": len(self._steering_apply),
                "total_duration_ms": _milliseconds(finished - self._started),
                "model_calls": self._model_calls,
                "tool_calls": self._tool_calls,
                "tool_executions": self._tool_executions,
                "tool_output_events": self._tool_outputs,
                "cache_hits": self._cache_hits,
            }
