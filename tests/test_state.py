"""Unit and integration tests for the explicit Agent run state machine."""

import json

import pytest

from corecoder import Agent
from corecoder.agent import MAX_ROUNDS_TEXT
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.state import (
    AgentResult,
    ExitReason,
    InvalidStateTransition,
    RunState,
    RunStatus,
    VerificationEvidenceRequired,
)
from corecoder.tools.read import ReadFileTool
from corecoder.trace import MemoryTraceSink, TraceRecorder
from corecoder.verification import (
    VerificationEvidence,
    VerificationResult,
    VerificationStatus,
)


def test_explicit_transition_table_covers_full_verified_path():
    state = RunState.create("complete the task", ["tests pass"], run_id="run-1")

    state.transition(RunStatus.RUNNING)
    state.transition(RunStatus.WAITING_APPROVAL)
    state.transition(RunStatus.EXECUTING)
    state.transition(RunStatus.RUNNING)
    state.transition(RunStatus.VERIFYING)
    state.record_verification(
        VerificationResult(
            status=VerificationStatus.PASSED,
            verifier="test",
            evidence=(
                VerificationEvidence.create(
                    verifier="test",
                    kind="test_result",
                    source="offline-test",
                    criterion="tests pass",
                    passed=True,
                    summary="Offline tests passed.",
                ),
            ),
        )
    )
    state.transition(
        RunStatus.VERIFIED,
        exit_reason=ExitReason.SUCCESS_CRITERIA_MET,
        final_text="done",
    )

    assert state.status is RunStatus.VERIFIED
    assert state.exit_reason is ExitReason.SUCCESS_CRITERIA_MET
    assert state.finished_at is not None
    assert state.final_text == "done"


def test_verified_transition_requires_passed_external_evidence():
    sink = MemoryTraceSink()
    trace_run = TraceRecorder(sink).new_run("scripted")
    state = RunState.create("task", run_id=trace_run.run_id)
    state.transition(RunStatus.RUNNING)
    state.transition(RunStatus.VERIFYING)

    with pytest.raises(VerificationEvidenceRequired):
        state.transition(RunStatus.VERIFIED, trace_run=trace_run)

    assert state.status is RunStatus.VERIFYING
    assert sink.events[-1]["error_category"] == "verification_evidence_required"


def test_illegal_transition_raises_domain_error_and_is_traced():
    sink = MemoryTraceSink()
    trace_run = TraceRecorder(sink).new_run("scripted")
    state = RunState.create("task", run_id=trace_run.run_id)

    with pytest.raises(InvalidStateTransition, match="pending -> verified"):
        state.transition(
            RunStatus.VERIFIED,
            trace_run=trace_run,
            exit_reason=ExitReason.SUCCESS_CRITERIA_MET,
        )

    assert state.status is RunStatus.PENDING
    assert state.state_version == 1
    event = sink.events[-1]
    assert event["event_type"] == "state_transition"
    assert event["outcome"] == "rejected"
    assert event["error_category"] == "invalid_state_transition"
    assert event["details"] == {
        "from_status": "pending",
        "to_status": "verified",
        "before_state_version": 1,
        "after_state_version": 1,
    }


def test_agent_run_returns_json_serializable_unverified_result():
    agent = Agent(llm=ScriptedLLM([LLMResponse(content="I completed everything")]))

    result = agent.run({
        "goal": "do the work",
        "success_criteria": ["tests pass", "artifact exists"],
    })

    assert isinstance(result, AgentResult)
    assert result.status is RunStatus.UNVERIFIED
    assert result.exit_reason is ExitReason.MODEL_STOPPED_UNVERIFIED
    assert result.final_text == "I completed everything"
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["goal"] == "do the work"
    assert payload["success_criteria"] == ["tests pass", "artifact exists"]
    assert payload["status"] == "unverified"
    assert payload["exit_reason"] == "model_stopped_unverified"
    assert payload["model_text"] == "I completed everything"
    assert payload["verification_status"] == "not_run"
    assert payload["evidence"] == []
    assert payload["failed_criteria"] == ["tests pass", "artifact exists"]
    assert payload["finished_at"] is not None


def test_model_text_cannot_directly_set_verified_state():
    claim = '{"status":"VERIFIED","exit_reason":"success_criteria_met"}'
    result = Agent(llm=ScriptedLLM([LLMResponse(content=claim)])).run("finish")

    assert result.status is RunStatus.UNVERIFIED
    assert result.final_text == claim


def test_only_controller_verifier_can_mark_result_verified():
    snapshots = []

    def verifier(snapshot):
        snapshots.append(snapshot)
        return snapshot["goal"] == "verify me"

    result = Agent(llm=ScriptedLLM([LLMResponse(content="done")])).run({
        "goal": "verify me",
        "success_criteria": ["trusted verifier approves"],
        "verifier": verifier,
    })

    assert result.status is RunStatus.VERIFIED
    assert result.exit_reason is ExitReason.SUCCESS_CRITERIA_MET
    assert snapshots[0]["status"] == "verifying"


def test_verifier_receives_copy_and_cannot_mutate_live_state():
    def verifier(snapshot):
        snapshot["status"] = "verified"
        snapshot["evidence_refs"].append("invented")
        return False

    result = Agent(llm=ScriptedLLM([LLMResponse(content="done")])).run({
        "goal": "verify safely",
        "verifier": verifier,
    })

    assert result.status is RunStatus.NEEDS_ACTION
    assert result.exit_reason is ExitReason.VERIFICATION_FAILED
    assert result.state.evidence_refs == []


def test_tool_calls_move_from_pending_to_completed_and_add_evidence(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(
                id="read-1",
                name="read_file",
                arguments={"file_path": str(target)},
            )]),
            LLMResponse(content="read it"),
        ]),
        tools=[ReadFileTool()],
    )

    result = agent.run("read the file")

    assert result.state.pending_calls == []
    assert result.state.completed_calls == [{
        "tool_call_id": "read-1",
        "tool_name": "read_file",
        "round": 1,
        "outcome": "success",
        "error_category": None,
        "evidence_ref": "tool:read-1:read_file",
    }]
    assert result.state.evidence_refs == ["tool:read-1:read_file"]
    assert result.state.round == 2


def test_max_rounds_has_structured_exit_reason_and_chat_marks_unverified(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    call = ToolCall(
        id="read-1",
        name="read_file",
        arguments={"file_path": str(target)},
    )
    result = Agent(
        llm=ScriptedLLM([LLMResponse(tool_calls=[call])]),
        tools=[ReadFileTool()],
        max_rounds=1,
    ).run("keep reading")

    assert result.status is RunStatus.FAILED
    assert result.exit_reason is ExitReason.MAX_ROUNDS
    assert result.final_text == MAX_ROUNDS_TEXT

    legacy = Agent(
        llm=ScriptedLLM([LLMResponse(tool_calls=[call])]),
        tools=[ReadFileTool()],
        max_rounds=1,
    )
    assert legacy.chat("keep reading") == f"[未验证] {MAX_ROUNDS_TEXT}"


def test_run_structures_model_error_while_chat_still_raises():
    result = Agent(llm=ScriptedLLM([])).run("fail")

    assert result.status is RunStatus.FAILED
    assert result.exit_reason is ExitReason.MODEL_ERROR
    assert result.error_type == "RuntimeError"
    assert result.to_dict()["error_type"] == "RuntimeError"

    with pytest.raises(RuntimeError, match="ran out of turns"):
        Agent(llm=ScriptedLLM([])).chat("fail")


class _InterruptLLM:
    model = "interrupt"
    total_prompt_tokens = 0
    total_completion_tokens = 0
    estimated_cost = None

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        raise KeyboardInterrupt


def test_run_structures_interruption_while_chat_still_raises():
    result = Agent(llm=_InterruptLLM()).run("stop")

    assert result.status is RunStatus.CANCELLED
    assert result.exit_reason is ExitReason.INTERRUPTED
    assert result.error_type == "KeyboardInterrupt"

    with pytest.raises(KeyboardInterrupt):
        Agent(llm=_InterruptLLM()).chat("stop")
