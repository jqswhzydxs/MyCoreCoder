"""Deterministic safe-point tests for append and redirect steering."""

import json
import threading
from typing import ClassVar

import pytest

from corecoder import (
    Agent,
    AgentEventType,
    InvalidStateTransition,
    RunState,
    RunStatus,
    SteeringRejectedError,
)
from corecoder.agent import STEERING_MESSAGE_PREFIX
from corecoder.llm import LLMResponse, ToolCall
from corecoder.tools.base import Tool


class GatedRecordingLLM:
    model = "gated-steering-llm"
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
            raise RuntimeError("steering test LLM ran out of responses")
        response = self.responses.pop(0)
        if on_token and response.content:
            on_token(response.content)
        if on_stream_activity:
            for index, _call in enumerate(response.tool_calls):
                on_stream_activity({
                    "activity": "tool_call_detected",
                    "tool_index": index,
                })
        return response


class ProbeTool(Tool):
    name = "probe"
    description = "Record deterministic tool execution."
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
    parallel_safe = False

    def __init__(
        self,
        executions: list[str],
        *,
        parallel_safe: bool = False,
        all_started: threading.Event | None = None,
        release: threading.Event | None = None,
        expected_starts: int = 0,
    ) -> None:
        self.executions = executions
        self.parallel_safe = parallel_safe
        self.all_started = all_started
        self.release = release
        self.expected_starts = expected_starts
        self._lock = threading.Lock()
        self._starts = 0

    def execute(self, label: str) -> str:
        with self._lock:
            self.executions.append(label)
            self._starts += 1
            if self.all_started is not None and self._starts == self.expected_starts:
                self.all_started.set()
        if self.release is not None and not self.release.wait(5):
            raise RuntimeError("test did not release parallel tools")
        return f"completed {label}"


def _tool_response(*labels: str) -> LLMResponse:
    return LLMResponse(tool_calls=[
        ToolCall(f"call-{label}", "probe", {"label": label})
        for label in labels
    ])


def _next_event(handle, event_type: AgentEventType):
    for _ in range(50):
        event = handle.poll_event(timeout=0.1)
        if event is not None and event.event_type is event_type:
            return event
    raise AssertionError(f"event not observed: {event_type.value}")


def _steering_messages(messages: list[dict]) -> list[str]:
    return [
        message["content"].split("\n", 1)[1]
        for message in messages
        if message.get("role") == "user"
        and str(message.get("content", "")).startswith(STEERING_MESSAGE_PREFIX)
    ]


def test_steer_acknowledgement_does_not_wait_for_model_completion():
    llm = GatedRecordingLLM([
        LLMResponse(content="draft"),
        LLMResponse(content="final"),
    ])
    handle = Agent(llm=llm, tools=[]).start("initial task")

    assert llm.entered.wait(1)
    instruction_id = handle.steer("add the backend constraint")

    received = _next_event(handle, AgentEventType.STEERING_RECEIVED)
    assert received.public_data["instruction_id"] == instruction_id
    assert received.public_data["queued_at_safe_point"] is True
    assert not llm.release.is_set()
    assert not handle.is_done

    llm.release.set()
    assert handle.result(timeout=2).final_text == "final"


def test_append_during_model_allows_planned_tool_then_updates_next_model_call():
    executions: list[str] = []
    llm = GatedRecordingLLM([
        _tool_response("safe"),
        LLMResponse(content="final with append"),
    ])
    handle = Agent(llm=llm, tools=[ProbeTool(executions)]).start("initial task")

    assert llm.entered.wait(1)
    instruction_id = handle.steer("only inspect backend", mode="append")
    llm.release.set()
    result = handle.result(timeout=2)

    assert result.final_text == "final with append"
    assert executions == ["safe"]
    assert _steering_messages(llm.calls[1]) == ["only inspect backend"]
    assert result.state.steering_history[0]["instruction_id"] == instruction_id
    assert "only inspect backend" not in json.dumps(result.state.steering_history)


def test_redirect_during_model_skips_returned_tool_and_replans():
    executions: list[str] = []
    llm = GatedRecordingLLM([
        _tool_response("obsolete"),
        LLMResponse(content="redirected final"),
    ])
    handle = Agent(llm=llm, tools=[ProbeTool(executions)]).start("initial task")

    assert llm.entered.wait(1)
    handle.steer("switch to backend only", mode="redirect")
    llm.release.set()
    result = handle.result(timeout=2)
    events = list(handle.events(timeout=1))

    assert result.final_text == "redirected final"
    assert executions == []
    assert _steering_messages(llm.calls[1]) == ["switch to backend only"]
    event_types = [event.event_type for event in events]
    assert AgentEventType.STEERING_APPLIED in event_types
    assert AgentEventType.REPLANNING in event_types


def test_redirect_after_tool_planning_backfills_every_tool_response():
    executions: list[str] = []
    llm = GatedRecordingLLM([
        _tool_response("one", "two"),
        LLMResponse(content="new plan complete"),
    ])
    holder = {}
    redirected = threading.Event()

    def on_event(event):
        if event.event_type is AgentEventType.TOOL_PLANNED and not redirected.is_set():
            redirected.set()
            holder["handle"].steer("replace the old plan", mode="redirect")

    handle = Agent(llm=llm, tools=[ProbeTool(executions)]).start(
        "initial task",
        on_event=on_event,
    )
    holder["handle"] = handle
    assert llm.entered.wait(1)
    llm.release.set()
    result = handle.result(timeout=2)

    assert redirected.is_set()
    assert executions == []
    second_call = llm.calls[1]
    assistant_index = next(
        index for index, message in enumerate(second_call)
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    replies = second_call[assistant_index + 1:assistant_index + 3]
    assert [message["role"] for message in replies] == ["tool", "tool"]
    assert [message["tool_call_id"] for message in replies] == ["call-one", "call-two"]
    assert all("redirect" in message["content"] for message in replies)
    steering_message = second_call[assistant_index + 3]
    assert steering_message == {
        "role": "user",
        "content": f"{STEERING_MESSAGE_PREFIX}\nreplace the old plan",
    }
    assert result.state.pending_calls == []


def test_redirect_between_serial_tools_prevents_later_tool_start():
    executions: list[str] = []
    llm = GatedRecordingLLM([
        _tool_response("first", "second", "third"),
        LLMResponse(content="serial redirect complete"),
    ])
    holder = {}
    steered = threading.Event()

    def on_event(event):
        if (
            event.event_type is AgentEventType.TOOL_FINISHED
            and event.public_data.get("tool_call_id") == "call-first"
            and not steered.is_set()
        ):
            steered.set()
            holder["handle"].steer("stop remaining tools", mode="redirect")

    handle = Agent(llm=llm, tools=[ProbeTool(executions)]).start(
        "serial task",
        on_event=on_event,
    )
    holder["handle"] = handle
    assert llm.entered.wait(1)
    llm.release.set()
    result = handle.result(timeout=2)

    assert result.final_text == "serial redirect complete"
    assert executions == ["first"]
    tool_replies = [
        message for message in llm.calls[1] if message.get("role") == "tool"
    ]
    assert [item["tool_call_id"] for item in tool_replies] == [
        "call-first",
        "call-second",
        "call-third",
    ]
    assert "redirect" in tool_replies[1]["content"]
    assert "redirect" in tool_replies[2]["content"]


def test_redirect_during_parallel_batch_keeps_completed_effects_and_replans():
    executions: list[str] = []
    all_started = threading.Event()
    release_tools = threading.Event()
    tool = ProbeTool(
        executions,
        parallel_safe=True,
        all_started=all_started,
        release=release_tools,
        expected_starts=2,
    )
    llm = GatedRecordingLLM([
        _tool_response("left", "right"),
        LLMResponse(content="parallel redirect complete"),
    ])
    handle = Agent(llm=llm, tools=[tool]).start("parallel task")

    assert llm.entered.wait(1)
    llm.release.set()
    assert all_started.wait(1)
    handle.steer("use the completed reads but change direction", mode="redirect")
    release_tools.set()
    result = handle.result(timeout=2)

    assert sorted(executions) == ["left", "right"]
    assert _steering_messages(llm.calls[1]) == [
        "use the completed reads but change direction"
    ]
    assert result.final_text == "parallel redirect complete"


def test_multiple_steering_instructions_are_applied_in_receive_order():
    llm = GatedRecordingLLM([
        LLMResponse(content="draft"),
        LLMResponse(content="ordered final"),
    ])
    handle = Agent(llm=llm, tools=[]).start("ordered steering")

    assert llm.entered.wait(1)
    first_id = handle.steer("first addition")
    second_id = handle.steer("second addition")
    llm.release.set()
    result = handle.result(timeout=2)
    events = list(handle.events(timeout=1))

    assert _steering_messages(llm.calls[1]) == ["first addition", "second addition"]
    assert [item["instruction_id"] for item in result.state.steering_history] == [
        first_id,
        second_id,
    ]
    applied_ids = [
        event.public_data["instruction_id"]
        for event in events
        if event.event_type is AgentEventType.STEERING_APPLIED
    ]
    assert applied_ids == [first_id, second_id]


def test_invalid_and_post_completion_steering_are_rejected():
    llm = GatedRecordingLLM([LLMResponse(content="done")])
    handle = Agent(llm=llm, tools=[]).start("short task")

    assert llm.entered.wait(1)
    with pytest.raises(ValueError, match="must not be empty"):
        handle.steer("   ")
    with pytest.raises(ValueError, match="exceeds"):
        handle.steer("x" * 32_769)

    llm.release.set()
    assert handle.result(timeout=2).final_text == "done"
    with pytest.raises(SteeringRejectedError, match="no longer accepts"):
        handle.steer("too late")


def test_pending_state_cannot_jump_directly_to_replanning():
    state = RunState.create("task")

    with pytest.raises(InvalidStateTransition, match="pending -> replanning"):
        state.transition(RunStatus.REPLANNING)

    state.transition(RunStatus.RUNNING)
    state.transition(RunStatus.REPLANNING)
    state.transition(RunStatus.RUNNING)
    assert state.current_step == "running"
