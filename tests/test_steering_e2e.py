"""End-to-end steering control-log and recovery tests."""

from __future__ import annotations

import threading
from typing import ClassVar

from corecoder import Agent, AgentEventType, RunState, RunStatus, SteeringMode
from corecoder.llm import LLMResponse, ToolCall
from corecoder.run_store import RunStore
from corecoder.tools.base import Tool


class GatedLLM:
    model = "steering-e2e-llm"
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
            raise RuntimeError("steering e2e LLM ran out of responses")
        response = self.responses.pop(0)
        if on_token and response.content:
            on_token(response.content)
        return response


class ProbeTool(Tool):
    name = "probe"
    description = "Record an execution for steering tests."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    }
    side_effect = "none"
    risk = "low"
    requires_confirmation = False
    idempotent = True
    parallel_safe = False

    def __init__(self, executions: list[str]) -> None:
        self.executions = executions

    def execute(self, label: str) -> str:
        self.executions.append(label)
        return f"completed {label}"


def _next_event(handle, event_type: AgentEventType):
    for _ in range(100):
        event = handle.poll_event(timeout=0.1)
        if event is not None and event.event_type is event_type:
            return event
    raise AssertionError(f"event not observed: {event_type.value}")


def test_control_log_records_received_applied_sequences_and_final_status(tmp_path):
    llm = GatedLLM([LLMResponse(content="draft"), LLMResponse(content="final")])
    store = RunStore(tmp_path / "runs.sqlite3")
    agent = Agent(llm=llm, tools=[], run_store=store)
    handle = agent.start("initial task")
    assert llm.entered.wait(1)

    instruction_id = handle.steer("append the backend constraint")
    received = _next_event(handle, AgentEventType.STEERING_RECEIVED)
    assert received.public_data["instruction_id"] == instruction_id

    llm.release.set()
    result = handle.result(timeout=3)
    rows = store.steering_controls(result.run_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.instruction_id == instruction_id
    assert row.mode == "append"
    assert row.status == "applied"
    assert row.received_sequence < row.applied_sequence
    assert row.received_at
    assert row.applied_at
    assert row.safe_point == "before_verification"
    assert row.run_status == result.status.value
    with store._transaction() as conn:
        control_text = " ".join(
            str(value)
            for control in conn.execute(
                "SELECT * FROM steering_controls WHERE run_id = ?",
                (result.run_id,),
            ).fetchall()
            for value in control
            if value is not None
        )
    assert "append the backend constraint" not in control_text


def test_unapplied_steering_becomes_needs_action_instead_of_silent_replay(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    state = RunState.create("recover me", [])
    state.transition(RunStatus.RUNNING)
    store.save_boundary(state, [{"role": "user", "content": "recover me"}], "running")
    store.record_steering_received(
        state.run_id,
        "received-only",
        mode="append",
        received_sequence=1,
    )

    llm = GatedLLM([LLMResponse(content="must not run")])
    result = Agent(llm=llm, tools=[], run_store=store).resume_run(
        state.run_id, resume_paused=True
    )

    assert result.status is RunStatus.NEEDS_ACTION
    assert "not persisted" in result.final_text
    assert llm.calls == []
    rows = store.steering_controls(state.run_id)
    assert rows[0].status == "needs_action"
    assert rows[0].run_status == result.status.value
    assert rows[0].needs_action_reason


def test_terminal_finalize_marks_unapplied_controls_needs_action(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    run_id = "run-finalize"
    store.record_steering_received(
        run_id, "never-applied", mode="redirect", received_sequence=2
    )
    store.record_steering_received(
        run_id, "already-applied", mode="append", received_sequence=3
    )
    store.mark_steering_applied(
        run_id,
        "already-applied",
        applied_sequence=4,
        safe_point="before_model",
    )

    store.finalize_steering(run_id, "cancelled")

    rows = {row.instruction_id: row for row in store.steering_controls(run_id)}
    assert rows["never-applied"].status == "needs_action"
    assert rows["never-applied"].needs_action_reason
    assert rows["never-applied"].run_status == "cancelled"
    assert rows["already-applied"].status == "applied"
    assert rows["already-applied"].applied_sequence == 4


def test_paused_state_defaults_to_staying_paused_during_recovery():
    state = RunState.create("pause me", [])
    state.transition(RunStatus.RUNNING)
    state.transition(RunStatus.PAUSED, current_step="paused:before_model")
    before = state.state_version

    state.prepare_resume()

    assert state.status is RunStatus.PAUSED
    assert state.current_step == "paused_recovered"
    assert state.state_version == before + 1

    state.prepare_resume(resume_paused=True)
    assert state.status is RunStatus.RUNNING


def test_redirect_skips_unstarted_calls_and_persists_skip_count(tmp_path):
    executions: list[str] = []
    tool = ProbeTool(executions)
    llm = GatedLLM([
        LLMResponse(tool_calls=[
            ToolCall("call-a", "probe", {"label": "front"}),
            ToolCall("call-b", "probe", {"label": "back"}),
        ]),
        LLMResponse(content="backend only"),
    ])
    holder: dict[str, object] = {}
    store = RunStore(tmp_path / "runs.sqlite3")

    def on_event(event):
        if event.event_type is AgentEventType.TOOL_PLANNED and "handle" in holder:
            holder["handle"].steer("only inspect the backend", SteeringMode.REDIRECT)

    agent = Agent(llm=llm, tools=[tool], run_store=store)
    handle = agent.start("inspect front and back", on_event=on_event)
    holder["handle"] = handle
    assert llm.entered.wait(1)
    llm.release.set()

    result = handle.result(timeout=3)
    assert result.status is RunStatus.VERIFIED or result.status is RunStatus.UNVERIFIED
    assert executions == []
    row = store.steering_controls(result.run_id)[0]
    assert row.mode == "redirect"
    assert row.status == "applied"
    assert row.skipped_tool_count == 2
    assistant = [
        message for message in agent.messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    replies = [
        message for message in agent.messages
        if message.get("role") == "tool"
    ]
    assert len(assistant) == 1
    assert {message["tool_call_id"] for message in replies} == {"call-a", "call-b"}
