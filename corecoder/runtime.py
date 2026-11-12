"""Backward-compatible background execution for :class:`corecoder.Agent`.

The synchronous Agent loop remains the single source of execution semantics.
This module only owns a worker thread, cooperative cancellation, result
handoff, and a bounded event buffer.

Backpressure policy
-------------------
Consecutive ``assistant_token`` events are coalesced up to a fixed character
limit without changing their concatenated text.  The in-memory queue has a
fixed event capacity.  Once it is full, the producer waits for the consumer;
lifecycle events are never dropped or displaced by tokens.  This deliberately
prefers a bounded process over an unbounded observer backlog.  ``close()`` is
the explicit abandonment path: it requests cancellation and releases a worker
that may be waiting on the event buffer.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

from .budget import CancellationToken, RunBudget
from .events import (
    AgentEvent,
    AgentEventType,
    CompositeEventSink,
    EventEmitter,
    EventSink,
    build_event_sink,
)
from .metrics import RunMetrics
from .state import AgentResult, RunStatus

if TYPE_CHECKING:
    from .agent import Agent


_TERMINAL_EVENT_TYPES = frozenset({
    AgentEventType.RUN_FINISHED,
    AgentEventType.RUN_FAILED,
    AgentEventType.RUN_CANCELLED,
})
_MAX_STEERING_TEXT_CHARS = 32_768


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class AgentBusyError(RuntimeError):
    """Raised when an Agent already has an active synchronous/background run."""


class RunControlRejectedError(RuntimeError):
    """Raised when a completed run can no longer accept control requests."""


class SteeringRejectedError(RunControlRejectedError):
    """Raised when a run no longer accepts steering instructions."""


class SteeringMode(str, Enum):
    APPEND = "append"
    REDIRECT = "redirect"


@dataclass(frozen=True, slots=True)
class SteeringInstruction:
    instruction_id: str
    text: str
    mode: SteeringMode
    created_at: str
    received_state_version: int
    received_round: int
    received_monotonic: float = field(
        default_factory=time.monotonic,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("steering text must be a string")
        clean_text = self.text.strip()
        if not clean_text:
            raise ValueError("steering text must not be empty")
        if len(clean_text) > _MAX_STEERING_TEXT_CHARS:
            raise ValueError(
                f"steering text exceeds {_MAX_STEERING_TEXT_CHARS} characters"
            )
        if self.received_state_version < 0 or self.received_round < 0:
            raise ValueError("received steering position must not be negative")
        object.__setattr__(self, "text", clean_text)
        object.__setattr__(self, "mode", SteeringMode(self.mode))


class ControlInbox:
    """Thread-safe steering queue and condition-based run control plane."""

    def __init__(self, *, max_text_chars: int = _MAX_STEERING_TEXT_CHARS) -> None:
        self._max_text_chars = max_text_chars
        self._items: deque[SteeringInstruction] = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._terminal_closed = False
        self._pause_requested = False
        self._paused = False
        self._resume_requested = False
        self._pause_reason: str | None = None
        self._cancel_requested = False
        self._state_version = 0
        self._round = 0

    def update_position(self, state_version: int, round_no: int) -> None:
        with self._condition:
            self._state_version = max(0, int(state_version))
            self._round = max(0, int(round_no))

    def submit(
        self,
        text: str,
        mode: SteeringMode | str = SteeringMode.APPEND,
    ) -> SteeringInstruction:
        if not isinstance(text, str):
            raise TypeError("steering text must be a string")
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("steering text must not be empty")
        if len(clean_text) > self._max_text_chars:
            raise ValueError(
                f"steering text exceeds {self._max_text_chars} characters"
            )
        selected_mode = SteeringMode(mode)
        with self._condition:
            if self._closed:
                raise SteeringRejectedError(
                    "This Agent run is finishing or finished and no longer accepts steering."
                )
            instruction = SteeringInstruction(
                instruction_id=uuid.uuid4().hex,
                text=clean_text,
                mode=selected_mode,
                created_at=_utc_now(),
                received_state_version=self._state_version,
                received_round=self._round,
            )
            self._items.append(instruction)
            return instruction

    def take(
        self,
        *,
        redirect_only: bool = False,
        close_if_empty: bool = False,
    ) -> list[SteeringInstruction]:
        """Take pending controls in receive order.

        With ``redirect_only``, append-only controls stay queued so an
        outstanding assistant tool-call message remains provider-valid until
        all of its tool replies exist.  A redirect drains the whole FIFO since
        it first backfills those replies and can then safely apply every item.
        """
        with self._condition:
            if not self._items:
                if close_if_empty and not self._pause_requested and not self._paused:
                    self._closed = True
                    self._condition.notify_all()
                return []
            if redirect_only and not any(
                item.mode is SteeringMode.REDIRECT for item in self._items
            ):
                return []
            instructions = list(self._items)
            self._items.clear()
            return instructions

    def request_pause(self, reason: str | None = None) -> bool:
        """Record a cooperative pause request; return false when already pending."""
        with self._condition:
            self._ensure_accepting("pause")
            if self._cancel_requested:
                raise RunControlRejectedError(
                    "This Agent run is cancelling and no longer accepts pause requests."
                )
            if (self._pause_requested or self._paused) and not self._resume_requested:
                return False
            self._resume_requested = False
            self._pause_requested = True
            self._pause_reason = str(reason) if reason is not None else None
            return True

    def request_resume(self) -> bool:
        """Clear a pending/effective pause and wake the Agent worker."""
        with self._condition:
            self._ensure_accepting("resume")
            if self._cancel_requested:
                raise RunControlRejectedError(
                    "This Agent run is cancelling and cannot be resumed."
                )
            if self._resume_requested:
                return False
            if not self._pause_requested and not self._paused:
                return False
            self._pause_requested = False
            self._resume_requested = True
            self._pause_reason = None
            self._condition.notify_all()
            return True

    def acknowledge_pause(self) -> tuple[bool, bool]:
        """Mark a requested pause effective at a safe point.

        The second return value only reports whether a private reason exists;
        the reason itself never crosses into a public event.
        """
        with self._condition:
            if (
                self._closed
                or self._cancel_requested
                or not self._pause_requested
            ):
                return False, False
            self._paused = True
            return True, bool(self._pause_reason)

    def wait_until_released(self, cancellation_token: CancellationToken) -> bool:
        """Wait without polling; return true only for an ordinary resume."""
        with self._condition:
            while (
                self._paused
                and self._pause_requested
                and not self._resume_requested
                and not self._cancel_requested
                and not cancellation_token.is_cancelled
                and not self._closed
            ):
                self._condition.wait()
            resumed = (
                not self._cancel_requested
                and not cancellation_token.is_cancelled
                and not self._closed
            )
            self._paused = False
            self._pause_requested = False
            self._resume_requested = False
            self._pause_reason = None
            return resumed

    def request_cancel(self) -> bool:
        """Wake paused workers and report whether this is the first request."""
        with self._condition:
            first_request = not self._cancel_requested and not self._terminal_closed
            self._cancel_requested = True
            self._pause_requested = False
            self._resume_requested = False
            self._condition.notify_all()
            return first_request

    def _ensure_accepting(self, operation: str) -> None:
        if self._closed:
            raise RunControlRejectedError(
                f"This Agent run is finishing or finished and cannot {operation}."
            )

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._terminal_closed = True
            self._pause_requested = False
            self._resume_requested = False
            self._condition.notify_all()

    @property
    def is_closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def pending_steering_count(self) -> int:
        with self._condition:
            return len(self._items)


class _BoundedEventBuffer:
    """A bounded, lossless event buffer with small token coalescing."""

    def __init__(self, max_events: int, token_chunk_chars: int) -> None:
        if max_events < 2:
            raise ValueError("event_queue_size must be at least 2")
        if token_chunk_chars < 1:
            raise ValueError("token_chunk_chars must be positive")
        self._max_events = max_events
        self._token_chunk_chars = token_chunk_chars
        self._items: deque[AgentEvent] = deque()
        self._condition = threading.Condition()
        self._abandoned = False
        self._finished = False

    @staticmethod
    def _can_merge(left: AgentEvent, right: AgentEvent) -> bool:
        return (
            left.event_type is AgentEventType.ASSISTANT_TOKEN
            and right.event_type is AgentEventType.ASSISTANT_TOKEN
            and left.run_id == right.run_id
            and left.phase == right.phase
            and left.public_data == right.public_data
        )

    def emit(self, event: AgentEvent) -> None:
        with self._condition:
            if self._abandoned:
                if event.event_type in _TERMINAL_EVENT_TYPES:
                    self._items.clear()
                    self._items.append(event)
                    self._condition.notify_all()
                return

            if self._items and self._can_merge(self._items[-1], event):
                previous = self._items[-1]
                if len(previous.message) + len(event.message) <= self._token_chunk_chars:
                    self._items[-1] = replace(
                        event,
                        message=previous.message + event.message,
                    )
                    self._condition.notify_all()
                    return

            while len(self._items) >= self._max_events and not self._abandoned:
                self._condition.wait()

            if self._abandoned:
                if event.event_type in _TERMINAL_EVENT_TYPES:
                    self._items.clear()
                    self._items.append(event)
                    self._condition.notify_all()
                return
            self._items.append(event)
            self._condition.notify_all()

    def get(self, timeout: float | None) -> AgentEvent | None:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while not self._items:
                if self._abandoned or self._finished:
                    return None
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            event = self._items.popleft()
            self._condition.notify_all()
            return event

    def abandon(self) -> None:
        with self._condition:
            self._abandoned = True
            self._items.clear()
            self._condition.notify_all()

    def finish(self) -> None:
        with self._condition:
            self._finished = True
            self._condition.notify_all()


class _RunObservation:
    """Small event-derived live snapshot for UIs such as ``/status``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._status = "starting"
        self._current_step = "starting"
        self._round = 0
        self._started_tool_ids: set[str] = set()

    def observe(self, event: AgentEvent) -> None:
        data = event.public_data
        raw_round = data.get("round")
        with self._lock:
            if isinstance(raw_round, int):
                self._round = max(self._round, raw_round)
            event_type = event.event_type
            if event_type is AgentEventType.RUN_STARTED:
                self._status, self._current_step = "running", "starting"
            elif event_type is AgentEventType.MODEL_STARTED:
                self._status, self._current_step = "running", "calling_model"
            elif event_type is AgentEventType.TOOL_PLANNED:
                self._current_step = "planning_tools"
            elif event_type is AgentEventType.TOOL_STARTED:
                self._status, self._current_step = "executing", "executing_tools"
                call_id = data.get("tool_call_id")
                if call_id is not None:
                    self._started_tool_ids.add(str(call_id))
            elif event_type in {AgentEventType.TOOL_FINISHED, AgentEventType.TOOL_FAILED}:
                self._status, self._current_step = "running", "collecting_tool_results"
            elif event_type is AgentEventType.VERIFICATION_STARTED:
                self._status, self._current_step = "verifying", "verifying"
            elif event_type is AgentEventType.REPLANNING:
                self._status, self._current_step = "replanning", "replanning"
            elif event_type is AgentEventType.RUN_PAUSED:
                self._status = "paused"
                self._current_step = f"paused:{data.get('safe_point', 'safe_point')}"
            elif event_type is AgentEventType.RUN_RESUMED:
                self._status, self._current_step = "running", "model_loop"
            elif event_type is AgentEventType.CANCELLATION_REQUESTED:
                self._status = "cancelling"
            elif event_type in _TERMINAL_EVENT_TYPES:
                self._status = str(data.get("status") or event_type.value.removeprefix("run_"))
                self._current_step = "finished"

    def snapshot(self, *, pending_steering_count: int) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "current_step": self._current_step,
                "round": self._round,
                "executed_tool_count": len(self._started_tool_ids),
                "elapsed_seconds": max(0.0, time.monotonic() - self._started),
                "pending_steering_count": pending_steering_count,
            }


class _HandleEventSink:
    """Track delivery state so a worker cannot disappear without a terminal event."""

    def __init__(
        self,
        buffer: _BoundedEventBuffer,
        control_inbox: ControlInbox,
        observation: _RunObservation,
    ) -> None:
        self._buffer = buffer
        self._control_inbox = control_inbox
        self._observation = observation
        self._lock = threading.Lock()
        self._terminal_seen = False

    def emit(self, event: AgentEvent) -> None:
        self._observation.observe(event)
        with self._lock:
            if event.event_type in _TERMINAL_EVENT_TYPES:
                self._terminal_seen = True
                self._control_inbox.close()
        self._buffer.emit(event)

    @property
    def terminal_seen(self) -> bool:
        with self._lock:
            return self._terminal_seen


class AgentRunHandle:
    """A thread-safe view of one background ``Agent.run()`` invocation."""

    def __init__(
        self,
        run_id: str,
        cancellation_token: CancellationToken,
        event_buffer: _BoundedEventBuffer,
        control_inbox: ControlInbox,
        event_emitter: EventEmitter,
        observation: _RunObservation,
        permission_broker=None,
        steering_recorder: Callable[[SteeringInstruction, int], None] | None = None,
    ) -> None:
        self.run_id = run_id
        self._cancellation_token = cancellation_token
        self._event_buffer = event_buffer
        self._control_inbox = control_inbox
        self._event_emitter = event_emitter
        self._observation = observation
        self._permission_broker = permission_broker
        self._steering_recorder = steering_recorder
        self._done = threading.Event()
        self._state_lock = threading.Lock()
        self._result: AgentResult | None = None
        self._exception: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def is_running(self) -> bool:
        return not self._done.is_set()

    @property
    def is_done(self) -> bool:
        return self._done.is_set()

    def poll_event(self, timeout: float | None = None) -> AgentEvent | None:
        """Return the next event, or ``None`` after the requested wait expires."""
        return self._event_buffer.get(timeout)

    def events(self, timeout: float | None = None) -> Iterator[AgentEvent]:
        """Yield events through the terminal event.

        When ``timeout`` is provided, an idle period of that length ends this
        iterator.  A later call may continue consuming remaining events.
        """
        while True:
            event = self.poll_event(timeout)
            if event is None:
                return
            yield event
            if event.event_type in _TERMINAL_EVENT_TYPES:
                return

    def status_snapshot(self) -> dict:
        """Return an event-derived, thread-safe live status projection."""
        snapshot = self._observation.snapshot(
            pending_steering_count=self._control_inbox.pending_steering_count,
        )
        snapshot["run_id"] = self.run_id
        return snapshot

    def pause(self, reason: str | None = None) -> bool:
        """Request a pause that becomes effective at the next safe point."""
        with self._event_emitter.synchronized():
            requested = self._control_inbox.request_pause(reason)
            if requested:
                self._event_emitter.emit(
                    AgentEventType.PAUSE_REQUESTED,
                    phase="control",
                    message="Pause requested; queued at a safe point",
                    public_data={
                        "queued_at_safe_point": True,
                        "has_reason": reason is not None,
                    },
                )
            return requested

    def resume(self) -> bool:
        """Release a pending/effective pause; the worker emits ``run_resumed``."""
        with self._event_emitter.synchronized():
            return self._control_inbox.request_resume()

    def cancel(self, reason: str | None = None) -> bool:
        """Request cancellation now; completion is confirmed by ``run_cancelled``."""
        with self._event_emitter.synchronized():
            self._cancellation_token.cancel(reason)
            requested = self._control_inbox.request_cancel()
            if self._permission_broker is not None:
                self._permission_broker.deny_all()
            if requested:
                self._event_emitter.emit(
                    AgentEventType.CANCELLATION_REQUESTED,
                    phase="control",
                    message="Cancellation requested; waiting for a safe stop",
                    public_data={"has_reason": reason is not None},
                )
            return requested

    def steer(
        self,
        text: str,
        mode: SteeringMode | str = SteeringMode.APPEND,
    ) -> str:
        """Queue a control and immediately acknowledge receipt, not application."""
        ack_started = time.monotonic()
        with self._event_emitter.synchronized():
            instruction = self._control_inbox.submit(text, mode)
            ack_ms = round((time.monotonic() - ack_started) * 1000.0, 3)
            received_event = self._event_emitter.emit(
                AgentEventType.STEERING_RECEIVED,
                phase="control",
                message="Steering instruction received; queued at a safe point",
                public_data={
                    "instruction_id": instruction.instruction_id,
                    "mode": instruction.mode.value,
                    "queued_at_safe_point": True,
                    "received_state_version": instruction.received_state_version,
                    "received_round": instruction.received_round,
                    "ack_ms": ack_ms,
                },
            )
            if self._steering_recorder is not None:
                # Persist only after the acknowledgement sequence exists.  The
                # body stays in ControlInbox memory and is never sent to the
                # run database.
                self._steering_recorder(instruction, received_event.sequence)
        return instruction.instruction_id

    def result(self, timeout: float | None = None) -> AgentResult:
        """Wait for completion, returning AgentResult or re-raising worker failure."""
        if not self._done.wait(timeout):
            raise TimeoutError(f"Agent run {self.run_id} did not finish before timeout")
        with self._state_lock:
            result = self._result
            error = self._exception
        if error is not None:
            raise error
        if result is None:
            raise RuntimeError(f"Agent run {self.run_id} finished without a result")
        return result

    def exception(self) -> BaseException | None:
        """Return a captured failure without blocking, including AgentResult errors."""
        if not self._done.is_set():
            return None
        with self._state_lock:
            if self._exception is not None:
                return self._exception
            return self._result._exception if self._result is not None else None

    def close(self) -> None:
        """Abandon event consumption and cooperatively cancel an active run."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._event_buffer.abandon()
        if not self._done.is_set():
            self.cancel("AgentRunHandle was closed by the caller.")
        self._control_inbox.close()

    def _attach_thread(self, thread: threading.Thread) -> None:
        self._thread = thread

    def _set_result(self, result: AgentResult) -> None:
        with self._state_lock:
            self._result = result

    def _set_exception(self, error: BaseException) -> None:
        with self._state_lock:
            self._exception = error

    def _mark_done(self) -> None:
        self._done.set()
        self._event_buffer.finish()


def start_agent_run(
    agent: Agent,
    task_spec,
    on_token: Callable | None = None,
    on_tool: Callable | None = None,
    on_reasoning: Callable | None = None,
    *,
    on_event: Callable[[AgentEvent], None] | None = None,
    event_sink: EventSink | None = None,
    budget: RunBudget | None = None,
    cancellation_token: CancellationToken | None = None,
    event_queue_size: int = 256,
    token_chunk_chars: int = 4096,
) -> AgentRunHandle:
    """Start the existing Agent loop in one worker after atomically claiming it."""
    token = cancellation_token or CancellationToken()
    event_buffer = _BoundedEventBuffer(event_queue_size, token_chunk_chars)
    control_inbox = ControlInbox()
    observation = _RunObservation()
    handle_sink = _HandleEventSink(event_buffer, control_inbox, observation)
    caller_sink = build_event_sink(on_event=on_event, event_sink=event_sink)
    combined_sink = CompositeEventSink(handle_sink, caller_sink)
    run_id = uuid.uuid4().hex
    event_emitter = EventEmitter(run_id, combined_sink)
    metrics = RunMetrics()
    event_emitter.add_observer(metrics.observe)
    run_store = getattr(agent, "run_store", None)
    steering_recorder = None
    if run_store is not None:
        def _record_steering(
            instruction: SteeringInstruction,
            sequence: int,
        ) -> None:
            run_store.record_steering_received(
                run_id,
                instruction.instruction_id,
                mode=instruction.mode.value,
                received_sequence=sequence,
                received_at=instruction.created_at,
            )

        steering_recorder = _record_steering

    handle = AgentRunHandle(
        run_id,
        token,
        event_buffer,
        control_inbox,
        event_emitter,
        observation,
        permission_broker=getattr(getattr(agent, "permission", None), "broker", None),
        steering_recorder=steering_recorder,
    )

    run_claim = agent._claim_active_run()

    def worker() -> None:
        result: AgentResult | None = None
        error: BaseException | None = None
        try:
            result = agent.run(
                task_spec,
                on_token=on_token,
                on_tool=on_tool,
                on_reasoning=on_reasoning,
                event_sink=combined_sink,
                budget=budget,
                cancellation_token=token,
                _run_id=run_id,
                _run_claim=run_claim,
                _event_emitter=event_emitter,
                _control_inbox=control_inbox,
                _metrics=metrics,
            )
            handle._set_result(result)
        except BaseException as exc:  # noqa: BLE001 - never let a worker die silently
            error = exc
            handle._set_exception(exc)
        finally:
            if not handle_sink.terminal_seen:
                if error is not None:
                    terminal_type = AgentEventType.RUN_FAILED
                elif token.is_cancelled:
                    terminal_type = AgentEventType.RUN_CANCELLED
                elif result is not None and result.status is not RunStatus.FAILED:
                    terminal_type = AgentEventType.RUN_FINISHED
                else:
                    terminal_type = AgentEventType.RUN_FAILED
                event_emitter.emit(
                    terminal_type,
                    phase=(
                        "failed"
                        if terminal_type is AgentEventType.RUN_FAILED
                        else "cancelled"
                        if terminal_type is AgentEventType.RUN_CANCELLED
                        else "finished"
                    ),
                    message=(
                        "Run failed"
                        if terminal_type is AgentEventType.RUN_FAILED
                        else "Run cancelled"
                        if terminal_type is AgentEventType.RUN_CANCELLED
                        else "Run finished"
                    ),
                    public_data={
                        "runtime_fallback": True,
                        "error_category": (
                            type(error).__name__ if error is not None else None
                        ),
                    },
                )
            control_inbox.close()
            agent._release_active_run(run_claim)
            handle._mark_done()

    thread = threading.Thread(
        target=worker,
        name=f"corecoder-run-{run_id[:8]}",
        daemon=True,
    )
    handle._attach_thread(thread)
    try:
        thread.start()
    except BaseException:
        agent._release_active_run(run_claim)
        raise
    return handle


__all__ = [
    "AgentBusyError",
    "AgentRunHandle",
    "ControlInbox",
    "RunControlRejectedError",
    "SteeringInstruction",
    "SteeringMode",
    "SteeringRejectedError",
]
