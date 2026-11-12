"""Coverage for content-free metrics and repeated-exploration reminders."""

from __future__ import annotations

import threading
from typing import ClassVar

from corecoder import Agent
from corecoder.budget import RunBudget
from corecoder.llm import LLMResponse, ToolCall
from corecoder.state import ExitReason
from corecoder.tools.base import Tool


class ScriptedLLM:
    model = "no-progress-test"
    total_prompt_tokens = 0
    total_completion_tokens = 0
    estimated_cost = None

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        self.calls += 1
        return self.responses.pop(0)


class ConstantReadTool(Tool):
    name = "constant_read"
    description = "Return a stable read-only result."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    side_effect = "read"
    risk = "low"
    required_permissions = ()
    requires_confirmation = False
    idempotent = True
    parallel_safe = True

    def __init__(self):
        self.calls = 0

    def execute(self, path: str):
        self.calls += 1
        return f"stable:{path}"


class GatedLLM:
    model = "gated-metrics-test"
    total_prompt_tokens = 0
    total_completion_tokens = 0
    estimated_cost = None

    def __init__(self, responses):
        self.responses = list(responses)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("test did not release the first model call")
        return self.responses.pop(0)


def call(call_id: str, name: str, **arguments):
    return ToolCall(id=call_id, name=name, arguments=arguments)


def test_repeated_read_only_call_gets_structured_replan_reminder():
    tool = ConstantReadTool()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[call("one", tool.name, path="a.py")]),
        LLMResponse(tool_calls=[call("two", tool.name, path="a.py")]),
        LLMResponse(content="summary complete"),
    ])
    agent = Agent(llm=llm, tools=[tool])

    result = agent.run(
        "inspect a.py",
        budget=RunBudget(max_rounds=5, max_no_progress_steps=5),
    )

    assert result.exit_reason is ExitReason.MODEL_STOPPED_UNVERIFIED
    reminders = [
        message["content"]
        for message in agent.messages
        if message.get("role") == "user"
        and str(message.get("content", "")).startswith("[exploration_reminder]")
    ]
    assert len(reminders) == 1
    assert "Summarize the evidence" in reminders[0]
    assert "Re-plan" in reminders[0]
    assert "glob" in reminders[0]
    assert "read_file" in reminders[0]
    assert tool.calls == 1


def test_different_read_arguments_are_not_treated_as_the_same_action():
    tool = ConstantReadTool()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[call("one", tool.name, path="a.py")]),
        LLMResponse(tool_calls=[call("two", tool.name, path="b.py")]),
        LLMResponse(content="done"),
    ])
    agent = Agent(llm=llm, tools=[tool])

    result = agent.run("inspect both", budget=RunBudget(max_rounds=5))

    assert result.budget_usage["no_progress_steps"] == 0
    assert not any(
        str(message.get("content", "")).startswith("[exploration_reminder]")
        for message in agent.messages
    )
    assert tool.calls == 2


def test_run_metrics_are_present_and_do_not_copy_tool_payloads():
    tool = ConstantReadTool()
    secret = "DO-NOT-LEAK-3f6c"
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[call("one", tool.name, path=secret)]),
        LLMResponse(content="done"),
    ])
    agent = Agent(llm=llm, tools=[tool])

    result = agent.run("measure")
    metrics = result.metrics

    for key in (
        "time_to_first_event_ms",
        "time_to_first_model_activity_ms",
        "time_to_first_tool_output_ms",
        "model_duration_ms",
        "tool_queue_duration_ms",
        "tool_duration_ms",
        "steering_ack_ms",
        "steering_apply_ms",
        "total_duration_ms",
    ):
        assert key in metrics
    assert metrics["time_to_first_event_ms"] is not None
    assert metrics["time_to_first_model_activity_ms"] is not None
    assert metrics["time_to_first_tool_output_ms"] is None
    assert metrics["model_calls"] >= 1
    assert metrics["tool_calls"] == 1
    assert metrics["tool_executions"] == 1
    assert metrics["total_duration_ms"] >= 0
    assert secret not in repr(metrics)
    assert result.to_dict()["metrics"] == metrics


def test_steering_ack_and_apply_metrics_come_from_runtime_events():
    llm = GatedLLM([
        LLMResponse(content="first answer"),
        LLMResponse(content="revised answer"),
    ])
    agent = Agent(llm=llm, tools=[])

    handle = agent.start("measure steering")
    instruction_id = handle.steer("summarize instead", "append")
    assert llm.entered.wait(5)
    llm.release.set()
    result = handle.result(timeout=10)

    assert instruction_id
    assert result.metrics["steering_ack_count"] == 1
    assert result.metrics["steering_apply_count"] == 1
    assert result.metrics["steering_ack_ms"] is not None
    assert result.metrics["steering_ack_ms"] >= 0
    assert result.metrics["steering_apply_ms"] is not None
    assert result.metrics["steering_apply_ms"] >= 0
