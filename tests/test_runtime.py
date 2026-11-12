"""Deterministic coverage for the backward-compatible background runtime."""

import threading
from itertools import pairwise
from typing import ClassVar

import pytest

from corecoder import Agent, AgentBusyError, CancellationToken
from corecoder.demo import ScriptedLLM
from corecoder.events import AgentEventType
from corecoder.llm import LLMResponse, ToolCall
from corecoder.state import RunStatus
from corecoder.tools.base import Tool


class GateLLM:
    model = "gate-llm"
    total_prompt_tokens = 0
    total_completion_tokens = 0
    estimated_cost = None

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.thread_id = None
        self.calls = 0

    def chat(
        self,
        messages,
        tools=None,
        on_token=None,
        on_reasoning=None,
        on_stream_activity=None,
    ) -> LLMResponse:
        self.calls += 1
        self.thread_id = threading.get_ident()
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("test did not release GateLLM")
        content = f"answer-{self.calls}"
        if on_token:
            on_token(content)
        return LLMResponse(content=content)


class ExplodingTool(Tool):
    name = "explode"
    description = "Raise a deterministic tool error."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}
    side_effect = "none"
    risk = "low"
    required_permissions: ClassVar[tuple[str, ...]] = ()
    requires_confirmation = False
    idempotent = True

    def execute(self) -> str:
        raise RuntimeError("deterministic tool failure")


def test_start_returns_immediately_and_runs_agent_on_worker_thread():
    llm = GateLLM()
    caller_thread = threading.get_ident()
    handle = Agent(llm=llm, tools=[]).start("background task")

    try:
        assert llm.entered.wait(1)
        assert handle.is_running
        assert not handle.is_done
        assert llm.thread_id != caller_thread
    finally:
        llm.release.set()
        handle.result(timeout=2)


def test_events_arrive_before_completion_and_result_waits_for_agent_result():
    llm = GateLLM()
    handle = Agent(llm=llm, tools=[]).start("observable task")

    assert llm.entered.wait(1)
    event = handle.poll_event(timeout=1)
    assert event is not None
    assert event.event_type is AgentEventType.RUN_STARTED
    assert not handle.is_done
    with pytest.raises(TimeoutError):
        handle.result(timeout=0.01)

    llm.release.set()
    result = handle.result(timeout=2)

    assert result.run_id == handle.run_id
    assert result.final_text == "answer-1"
    assert handle.is_done
    assert not handle.is_running


def test_cancel_sets_the_existing_cancellation_token():
    llm = GateLLM()
    token = CancellationToken()
    handle = Agent(llm=llm, tools=[]).start(
        "cancel task",
        cancellation_token=token,
    )

    assert llm.entered.wait(1)
    handle.cancel("changed requirements")
    assert token.is_cancelled
    assert token.reason == "changed requirements"

    llm.release.set()
    result = handle.result(timeout=2)
    assert result.status is RunStatus.CANCELLED
    assert list(handle.events(timeout=1))[-1].event_type is AgentEventType.RUN_CANCELLED


def test_close_is_idempotent_and_does_not_strand_the_worker():
    llm = GateLLM()
    handle = Agent(llm=llm, tools=[]).start("close task", event_queue_size=2)

    assert llm.entered.wait(1)
    handle.close()
    handle.close()
    llm.release.set()

    assert handle.result(timeout=2).status is RunStatus.CANCELLED


def test_same_agent_rejects_concurrent_run_and_is_reusable_after_finish():
    llm = GateLLM()
    agent = Agent(llm=llm, tools=[])
    first = agent.start("first")

    assert llm.entered.wait(1)
    with pytest.raises(AgentBusyError, match="active run"):
        agent.start("second")
    with pytest.raises(AgentBusyError, match="active run"):
        agent.run("also second")

    llm.release.set()
    assert first.result(timeout=2).final_text == "answer-1"

    second = agent.start("second after finish")
    assert second.result(timeout=2).final_text == "answer-2"


def test_sink_model_and_tool_errors_never_leave_a_handle_waiting():
    class FailingSink:
        def emit(self, event):
            raise RuntimeError("observer failure")

    class FailingLLM:
        model = "failing-llm"
        total_prompt_tokens = 0
        total_completion_tokens = 0
        estimated_cost = None

        def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
            raise ValueError("model failure")

    model_handle = Agent(llm=FailingLLM(), tools=[]).start(
        "model fails",
        event_sink=FailingSink(),
    )
    model_result = model_handle.result(timeout=2)
    assert model_result.status is RunStatus.FAILED
    assert isinstance(model_handle.exception(), ValueError)
    assert list(model_handle.events(timeout=1))[-1].event_type is AgentEventType.RUN_FAILED

    tool_handle = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("boom-1", "explode", {})]),
            LLMResponse(content="recovered after tool error"),
        ]),
        tools=[ExplodingTool()],
    ).start("tool fails")
    tool_result = tool_handle.result(timeout=2)
    assert tool_result.final_text == "recovered after tool error"
    tool_events = list(tool_handle.events(timeout=1))
    assert AgentEventType.TOOL_FAILED in [event.event_type for event in tool_events]
    assert tool_events[-1].event_type is AgentEventType.RUN_FINISHED


def test_slow_consumer_gets_lifecycle_events_and_exact_coalesced_text():
    text = "streamed text " * 100

    class BurstLLM:
        model = "burst-llm"
        total_prompt_tokens = 0
        total_completion_tokens = 0
        estimated_cost = None

        def __init__(self) -> None:
            self.returning = threading.Event()

        def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
            for character in text:
                if on_token:
                    on_token(character)
            self.returning.set()
            return LLMResponse(content=text)

    llm = BurstLLM()
    handle = Agent(llm=llm, tools=[]).start(
        "burst",
        event_queue_size=4,
        token_chunk_chars=len(text) + 1,
    )

    # The model has produced everything before this consumer starts.  With a
    # four-event queue, later lifecycle events exercise producer backpressure.
    assert llm.returning.wait(1)
    events = list(handle.events(timeout=2))
    result = handle.result(timeout=2)

    event_types = [event.event_type for event in events]
    streamed = "".join(
        event.message
        for event in events
        if event.event_type is AgentEventType.ASSISTANT_TOKEN
    )
    assert result.final_text == text
    assert streamed == text
    assert AgentEventType.MODEL_FINISHED in event_types
    assert AgentEventType.VERIFICATION_STARTED in event_types
    assert AgentEventType.VERIFICATION_FINISHED in event_types
    assert event_types[-1] is AgentEventType.RUN_FINISHED
    assert all(
        left.sequence < right.sequence
        for left, right in pairwise(events)
    )


def test_unexpected_worker_failure_is_visible_and_releases_busy_state():
    agent = Agent(llm=ScriptedLLM([LLMResponse(content="later")]), tools=[])
    handle = agent.start(object())

    with pytest.raises(TypeError, match="task_spec"):
        handle.result(timeout=2)
    assert isinstance(handle.exception(), TypeError)
    events = list(handle.events(timeout=1))
    assert len(events) == 1
    assert events[0].event_type is AgentEventType.RUN_FAILED

    next_handle = agent.start("valid next run")
    assert next_handle.result(timeout=2).final_text == "later"
