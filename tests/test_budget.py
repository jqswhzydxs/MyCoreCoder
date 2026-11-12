"""Offline tests for run budgets, cancellation, and loop detection."""

from typing import ClassVar

import pytest

from corecoder import Agent, CancellationToken, RunBudget
from corecoder.budget import action_fingerprint, canonicalize_arguments
from corecoder.llm import LLMResponse, ToolCall
from corecoder.state import ExitReason, RunStatus
from corecoder.tools.base import Tool
from corecoder.tools.protocol import ToolResult
from corecoder.tools.write import WriteFileTool
from corecoder.trace import MemoryTraceSink


class UsageLLM:
    def __init__(self, responses, *, costs=None, on_call=None, known_cost=True):
        self.model = "priced-test" if known_cost else "unknown-price-test"
        self.responses = list(responses)
        self.costs = list(costs or [0.0] * len(self.responses))
        self.on_call = on_call
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.calls = 0
        self._cost = 0.0
        self._known_cost = known_cost

    @property
    def estimated_cost(self):
        return self._cost if self._known_cost else None

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        if self.on_call:
            self.on_call(self.calls)
        response = self.responses.pop(0)
        self.total_prompt_tokens += response.prompt_tokens
        self.total_completion_tokens += response.completion_tokens
        self._cost += self.costs[self.calls]
        self.calls += 1
        return response


class CancellingTool(Tool):
    name = "cancel_now"
    description = "Request cooperative cancellation during a tool call."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}
    side_effect = "none"
    risk = "low"
    required_permissions = ()
    requires_confirmation = False
    idempotent = True
    parallel_safe = False

    def __init__(self, token):
        self.token = token

    def execute(self):
        self.token.cancel("test caller requested cancellation")
        return "tool stopped at a safe boundary"


class ChangingResultTool(Tool):
    name = "changing_result"
    description = "Return new evidence for the same requested action."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}
    side_effect = "read"
    risk = "low"
    required_permissions = ()
    requires_confirmation = False
    idempotent = False
    parallel_safe = False

    def __init__(self):
        self.calls = 0

    def execute(self):
        self.calls += 1
        return f"new evidence {self.calls}"


def _call(call_id, name="missing_tool", **arguments):
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _assert_tool_history_is_paired(messages):
    requested = {
        call["id"]
        for message in messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls", [])
    }
    answered = {
        message["tool_call_id"]
        for message in messages
        if message.get("role") == "tool"
    }
    assert requested == answered


def test_explicit_max_rounds_stops_with_actionable_result():
    llm = UsageLLM([LLMResponse(tool_calls=[_call("one")])])
    result = Agent(llm=llm, tools=[]).run(
        "loop",
        budget=RunBudget(max_rounds=1),
    )

    assert result.exit_reason is ExitReason.MAX_ROUNDS
    assert result.status is RunStatus.FAILED
    assert "RunBudget.max_rounds" in result.final_text
    assert result.budget_usage["rounds"] == 1


def test_max_tool_calls_allows_final_model_but_refuses_next_tool():
    llm = UsageLLM([
        LLMResponse(tool_calls=[_call("one")]),
        LLMResponse(tool_calls=[_call("two")]),
    ])
    agent = Agent(llm=llm, tools=[])

    result = agent.run("loop", budget=RunBudget(max_tool_calls=1, max_rounds=5))

    assert result.exit_reason is ExitReason.MAX_TOOL_CALLS
    assert result.budget_usage["tool_calls"] == 1
    assert len(agent.tool_results) == 1
    _assert_tool_history_is_paired(agent.messages)


def test_max_tool_calls_can_be_exactly_used_then_model_can_finish():
    llm = UsageLLM([
        LLMResponse(tool_calls=[_call("one")]),
        LLMResponse(content="done"),
    ])

    result = Agent(llm=llm, tools=[]).run(
        "finish after one tool",
        budget=RunBudget(max_tool_calls=1, max_rounds=5),
    )

    assert result.exit_reason is ExitReason.MODEL_STOPPED_UNVERIFIED
    assert result.budget_usage["tool_calls"] == 1


@pytest.mark.parametrize(
    ("budget", "response", "reason"),
    [
        (
            RunBudget(max_prompt_tokens=4),
            LLMResponse(content="done", prompt_tokens=5),
            ExitReason.MAX_PROMPT_TOKENS,
        ),
        (
            RunBudget(max_completion_tokens=2),
            LLMResponse(content="done", completion_tokens=3),
            ExitReason.MAX_COMPLETION_TOKENS,
        ),
    ],
)
def test_token_budgets_are_checked_after_model_return(budget, response, reason):
    result = Agent(llm=UsageLLM([response]), tools=[]).run("task", budget=budget)

    assert result.exit_reason is reason
    assert result.status is RunStatus.FAILED
    assert reason.value in result.final_text


def test_cost_budget_uses_known_price_accounting():
    llm = UsageLLM([LLMResponse(content="done")], costs=[0.75])
    result = Agent(llm=llm, tools=[]).run(
        "task",
        budget=RunBudget(max_cost_usd=0.5),
    )

    assert result.exit_reason is ExitReason.MAX_COST_USD
    assert result.budget_usage["cost_usd"] == 0.75
    assert result.budget_usage["cost_unavailable"] is False


def test_unknown_model_price_records_unavailable_and_checks_other_budgets():
    sink = MemoryTraceSink()
    llm = UsageLLM(
        [LLMResponse(content="done", prompt_tokens=1)],
        known_cost=False,
    )
    result = Agent(llm=llm, tools=[], trace_sink=sink).run(
        "task",
        budget=RunBudget(max_cost_usd=0, max_prompt_tokens=10),
    )

    assert result.exit_reason is ExitReason.MODEL_STOPPED_UNVERIFIED
    assert result.budget_usage["cost_usd"] is None
    assert result.budget_usage["cost_unavailable"] is True
    checks = [event for event in sink.events if event["event_type"] == "budget_checked"]
    assert checks
    assert all(event["details"]["cost_unavailable"] is True for event in checks)


def test_zero_deadline_stops_before_any_model_call():
    llm = UsageLLM([LLMResponse(content="must not run")])
    result = Agent(llm=llm, tools=[]).run(
        "task",
        budget=RunBudget(deadline_seconds=0),
    )

    assert result.exit_reason is ExitReason.DEADLINE_EXCEEDED
    assert llm.calls == 0
    assert result.budget_usage["elapsed_seconds"] >= 0


def test_cancellation_after_tool_keeps_message_history_legal():
    token = CancellationToken()
    call = _call("cancel-1", name="cancel_now")
    agent = Agent(
        llm=UsageLLM([LLMResponse(tool_calls=[call])]),
        tools=[CancellingTool(token)],
    )

    result = agent.run("task", cancellation_token=token)

    assert result.status is RunStatus.CANCELLED
    assert result.exit_reason is ExitReason.CANCELLED
    assert "test caller requested cancellation" in result.final_text
    _assert_tool_history_is_paired(agent.messages)


def test_pre_cancelled_token_starts_no_work():
    token = CancellationToken()
    token.cancel("cancel before start")
    llm = UsageLLM([LLMResponse(content="must not run")])

    result = Agent(llm=llm, tools=[]).run("task", cancellation_token=token)

    assert result.exit_reason is ExitReason.CANCELLED
    assert llm.calls == 0


def test_repeated_failed_action_stops_at_no_progress_threshold():
    sink = MemoryTraceSink()
    llm = UsageLLM([
        LLMResponse(tool_calls=[_call("one", value=1)]),
        LLMResponse(tool_calls=[_call("two", value=1)]),
        LLMResponse(content="must not reach"),
    ])
    agent = Agent(llm=llm, tools=[], trace_sink=sink)

    result = agent.run(
        "loop",
        budget=RunBudget(max_rounds=10, max_no_progress_steps=1),
    )

    assert result.exit_reason is ExitReason.NO_PROGRESS
    assert result.budget_usage["no_progress_steps"] == 1
    assert llm.calls == 2
    assert len(agent.tool_results) == 2
    assert any(event["event_type"] == "no_progress" for event in sink.events)


def test_repeated_call_with_changed_result_is_progress_not_a_loop():
    tool = ChangingResultTool()
    llm = UsageLLM([
        LLMResponse(tool_calls=[_call("one", name=tool.name)]),
        LLMResponse(tool_calls=[_call("two", name=tool.name)]),
        LLMResponse(content="done"),
    ])

    result = Agent(llm=llm, tools=[tool]).run(
        "observe changes",
        budget=RunBudget(max_rounds=5, max_no_progress_steps=1),
    )

    assert result.exit_reason is ExitReason.MODEL_STOPPED_UNVERIFIED
    assert result.budget_usage["no_progress_steps"] == 0
    assert tool.calls == 2


def test_exhausted_prompt_budget_does_not_start_side_effect_tool(tmp_path):
    target = tmp_path / "must-not-exist.txt"
    call = _call(
        "write-1",
        name="write_file",
        file_path=str(target),
        content="unsafe",
    )
    agent = Agent(
        llm=UsageLLM([LLMResponse(tool_calls=[call], prompt_tokens=5)]),
        tools=[WriteFileTool()],
    )

    result = agent.run("write", budget=RunBudget(max_prompt_tokens=5))

    assert result.exit_reason is ExitReason.MAX_PROMPT_TOKENS
    assert not target.exists()
    assert result.budget_usage["tool_calls"] == 0
    _assert_tool_history_is_paired(agent.messages)


def test_action_fingerprint_is_canonical_and_result_sensitive():
    first = ToolResult.success("a", "echo", "same")
    second = ToolResult.success("b", "echo", "same")
    changed = ToolResult.success("c", "echo", "different")

    assert canonicalize_arguments({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert action_fingerprint("echo", {"b": 2, "a": 1}, first) == action_fingerprint(
        "echo",
        {"a": 1, "b": 2},
        second,
    )
    assert action_fingerprint("echo", {"a": 1, "b": 2}, first) != action_fingerprint(
        "echo",
        {"a": 1, "b": 2},
        changed,
    )
