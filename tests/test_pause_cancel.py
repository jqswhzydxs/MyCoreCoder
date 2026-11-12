"""Deterministic pause/resume/cancel coverage for background Agent runs."""

import threading
from typing import ClassVar

import pytest

from corecoder import (
    Agent,
    AgentEventType,
    InvalidStateTransition,
    RunControlRejectedError,
    RunState,
    RunStatus,
)
from corecoder.agent import STEERING_MESSAGE_PREFIX
from corecoder.llm import LLMResponse, ToolCall
from corecoder.tools.base import Tool


class GatedRecordingLLM:
    model = "pause-control-llm"
    total_prompt_tokens = 0
    total_completion_tokens = 0
    estimated_cost = None

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[list[dict]] = []

    def chat(
        self,
        messages,
        tools=None,
        on_token=None,
        on_reasoning=None,
        on_stream_activity=None,
    ) -> LLMResponse:
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("test did not release the first model call")
        if not self.responses:
            raise RuntimeError("pause-control LLM ran out of responses")
        response = self.responses.pop(0)
        if on_token and response.content:
            on_token(response.content)
        return response


class EntryGatedAgent(Agent):
    """Hold the worker before ``Agent._run_impl`` creates its first safe point."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.worker_entered = threading.Event()
        self.release_worker = threading.Event()

    def _run_impl(self, *args, **kwargs):
        self.worker_entered.set()
        if not self.release_worker.wait(5):
            raise RuntimeError("test did not release the Agent worker")
        return super()._run_impl(*args, **kwargs)


class ControlledTool(Tool):
    name = "controlled"
    description = "Record deterministic execution with optional gates."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    }
    side_effect = "none"
    risk = "low"
    required_permissions: ClassVar[tuple[str, ...]] = ()
    requires_confirmation = False
    idempotent = True

    def __init__(
        self,
        executions: list[str],
        *,
        parallel_safe: bool = False,
        block_label: str | None = None,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
        started_target: int | None = None,
    ) -> None:
        self.executions = executions
        self.parallel_safe = parallel_safe
        self.block_label = block_label
        self.entered = entered
        self.release = release
        self.started_target = started_target
        self._lock = threading.Lock()
        self._started = 0

    def execute(self, label: str) -> str:
        with self._lock:
            self.executions.append(label)
            self._started += 1
            should_signal = (
                self.entered is not None
                and (
                    self.started_target is None
                    or self._started >= self.started_target
                )
            )
            if should_signal:
                self.entered.set()
        if (
            self.release is not None
            and (self.block_label is None or label == self.block_label)
            and not self.release.wait(5)
        ):
            raise RuntimeError("test did not release controlled tool")
        return f"completed {label}"


def _tool_response(*labels: str) -> LLMResponse:
    return LLMResponse(tool_calls=[
        ToolCall(f"call-{label}", "controlled", {"label": label})
        for label in labels
    ])


def _events_until(handle, event_type: AgentEventType) -> list:
    observed = []
    for _ in range(100):
        event = handle.poll_event(timeout=0.1)
        if event is None:
            continue
        observed.append(event)
        if event.event_type is event_type:
            return observed
    raise AssertionError(f"event not observed: {event_type.value}")


def _steering_texts(messages: list[dict]) -> list[str]:
    return [
        str(message["content"]).split("\n", 1)[1]
        for message in messages
        if message.get("role") == "user"
        and str(message.get("content", "")).startswith(STEERING_MESSAGE_PREFIX)
    ]


def _pause_before_first_model():
    llm = GatedRecordingLLM([LLMResponse(content="final")])
    agent = EntryGatedAgent(llm=llm, tools=[])
    handle = agent.start("task")
    assert agent.worker_entered.wait(1)
    pause_results = [handle.pause("wait before the model")]
    handle.steer("first addition")
    agent.release_worker.set()
    observed = _events_until(handle, AgentEventType.RUN_PAUSED)
    return llm, handle, observed, pause_results


def test_before_model_pause_resume_and_steering_while_paused_are_ordered():
    llm, handle, observed, pause_results = _pause_before_first_model()

    assert pause_results == [True]
    assert observed[-1].public_data["safe_point"] == "before_model"
    assert AgentEventType.PAUSE_REQUESTED in [event.event_type for event in observed]
    assert len(llm.calls) == 0
    assert handle.pause("duplicate") is False
    handle.steer("second addition while paused")

    llm.release.set()
    assert handle.resume() is True
    assert handle.resume() is False
    result = handle.result(timeout=2)
    remaining = list(handle.events(timeout=1))

    assert result.final_text == "final"
    assert _steering_texts(llm.calls[0]) == [
        "first addition",
        "second addition while paused",
    ]
    assert AgentEventType.RUN_RESUMED in [event.event_type for event in remaining]


def test_pause_between_serial_tools_blocks_the_next_start_until_resume():
    executions: list[str] = []
    first_entered = threading.Event()
    release_first = threading.Event()
    tool = ControlledTool(
        executions,
        block_label="first",
        entered=first_entered,
        release=release_first,
    )
    llm = GatedRecordingLLM([
        _tool_response("first", "second"),
        LLMResponse(content="done"),
    ])
    holder = {}
    pause_results = []

    def on_event(event):
        if (
            event.event_type is AgentEventType.TOOL_FINISHED
            and event.public_data.get("tool_call_id") == "call-first"
        ):
            pause_results.append(holder["handle"].pause())

    handle = Agent(llm=llm, tools=[tool]).start("serial", on_event=on_event)
    holder["handle"] = handle
    assert llm.entered.wait(1)
    llm.release.set()
    assert first_entered.wait(1)
    release_first.set()
    observed = _events_until(handle, AgentEventType.RUN_PAUSED)

    assert pause_results == [True]
    assert observed[-1].public_data["safe_point"] == "after_tool"
    assert executions == ["first"]
    handle.resume()

    assert handle.result(timeout=2).final_text == "done"
    assert executions == ["first", "second"]


def test_cancel_while_paused_wakes_worker_and_finishes_cancelled():
    _llm, handle, observed, _pause_results = _pause_before_first_model()

    assert handle.cancel("requirements withdrawn") is True
    result = handle.result(timeout=2)
    remaining = list(handle.events(timeout=1))
    event_types = [event.event_type for event in observed + remaining]

    assert result.status is RunStatus.CANCELLED
    assert AgentEventType.CANCELLATION_REQUESTED in event_types
    assert event_types[-1] is AgentEventType.RUN_CANCELLED
    assert event_types.index(AgentEventType.CANCELLATION_REQUESTED) < event_types.index(
        AgentEventType.RUN_CANCELLED
    )


def test_finished_run_rejects_pause_and_resume_clearly():
    llm = GatedRecordingLLM([LLMResponse(content="done")])
    handle = Agent(llm=llm, tools=[]).start("short")
    assert llm.entered.wait(1)
    llm.release.set()
    assert handle.result(timeout=2).final_text == "done"

    with pytest.raises(RunControlRejectedError, match="cannot pause"):
        handle.pause()
    with pytest.raises(RunControlRejectedError, match="cannot resume"):
        handle.resume()


def test_cancel_during_model_does_not_start_another_model_call():
    llm = GatedRecordingLLM([
        LLMResponse(content="ignored"),
        LLMResponse(content="must not run"),
    ])
    handle = Agent(llm=llm, tools=[]).start("cancel model")
    assert llm.entered.wait(1)

    assert handle.cancel() is True
    assert handle.cancel() is False
    llm.release.set()
    result = handle.result(timeout=2)

    assert result.status is RunStatus.CANCELLED
    assert len(llm.calls) == 1


def test_cancel_after_planning_starts_no_tools_and_repairs_history():
    executions: list[str] = []
    llm = GatedRecordingLLM([_tool_response("one", "two")])
    agent = Agent(llm=llm, tools=[ControlledTool(executions)])
    holder = {}
    cancelled = threading.Event()

    def on_event(event):
        if event.event_type is AgentEventType.TOOL_PLANNED and not cancelled.is_set():
            cancelled.set()
            holder["handle"].cancel("stop planned tools")

    handle = agent.start("cancel tools", on_event=on_event)
    holder["handle"] = handle
    assert llm.entered.wait(1)
    llm.release.set()
    result = handle.result(timeout=2)

    assert result.status is RunStatus.CANCELLED
    assert executions == []
    replies = [message for message in agent.messages if message.get("role") == "tool"]
    assert [message["tool_call_id"] for message in replies] == [
        "call-one",
        "call-two",
    ]
    assert result.state.pending_calls == []


def test_parallel_cancel_marks_queued_calls_without_starting_them():
    executions: list[str] = []
    workers_started = threading.Event()
    release_workers = threading.Event()
    tool = ControlledTool(
        executions,
        parallel_safe=True,
        entered=workers_started,
        release=release_workers,
        started_target=8,
    )
    labels = [f"item-{index}" for index in range(10)]
    llm = GatedRecordingLLM([_tool_response(*labels)])
    handle = Agent(llm=llm, tools=[tool]).start("parallel cancellation")
    assert llm.entered.wait(1)
    llm.release.set()
    assert workers_started.wait(2)

    handle.cancel("stop queued work")
    release_workers.set()
    result = handle.result(timeout=3)

    assert result.status is RunStatus.CANCELLED
    assert len(executions) == 8
    assert len(result.state.completed_calls) == 10
    assert sum(
        call.get("error_category") == "scheduler_cancelled"
        for call in result.state.completed_calls
    ) == 2
    assert result.state.pending_calls == []


def test_paused_state_has_only_explicit_resume_or_cancel_transitions():
    state = RunState.create("state controls")
    with pytest.raises(InvalidStateTransition, match="pending -> paused"):
        state.transition(RunStatus.PAUSED)

    state.transition(RunStatus.RUNNING)
    state.transition(RunStatus.PAUSED)
    with pytest.raises(InvalidStateTransition, match="paused -> executing"):
        state.transition(RunStatus.EXECUTING)
    state.transition(RunStatus.RUNNING)
    assert state.status is RunStatus.RUNNING
