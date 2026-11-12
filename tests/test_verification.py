"""Offline tests for evidence-based, caller-controlled verification."""

import json
import sys

from corecoder import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.state import ExitReason, RunStatus
from corecoder.tools.read import ReadFileTool
from corecoder.trace import MemoryTraceSink
from corecoder.verification import (
    CompositeVerifier,
    FileStateVerifier,
    NoFailedToolCallVerifier,
    RequiredEvidenceVerifier,
    VerificationStatus,
)
from corecoder.verification import (
    TestResultVerifier as CoreTestResultVerifier,
)


def _exit_command(code: int) -> str:
    executable = sys.executable.replace("\\", "/")
    return f'"{executable}" -c "import sys; sys.exit({code})"'


def test_file_state_evidence_is_required_before_model_claim_becomes_verified(tmp_path):
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("ready\n", encoding="utf-8")
    verifier = FileStateVerifier(artifact, content_equals="ready\n")

    result = Agent(llm=ScriptedLLM([LLMResponse(content="Everything is done.")])).run({
        "goal": "produce the artifact",
        "success_criteria": ["artifact exists and contains ready"],
        "verifier": verifier,
    })

    assert result.status is RunStatus.VERIFIED
    assert result.verification_status is VerificationStatus.PASSED
    assert result.model_text == "Everything is done."
    assert result.display_text == result.model_text
    assert len(result.evidence) == 1
    assert result.evidence[0]["kind"] == "file_state"
    assert result.evidence[0]["passed"] is True
    assert result.failed_criteria == ()


def test_wrong_completion_claim_becomes_needs_action_not_verified(tmp_path):
    missing = tmp_path / "deliverable.txt"
    result = Agent(llm=ScriptedLLM([LLMResponse(content="I created it.")])).run({
        "goal": "create deliverable",
        "success_criteria": ["deliverable exists"],
        "verifier": FileStateVerifier(missing),
    })

    assert result.status is RunStatus.NEEDS_ACTION
    assert result.exit_reason is ExitReason.VERIFICATION_FAILED
    assert result.verification_status is VerificationStatus.FAILED
    assert result.failed_criteria
    assert result.display_text.startswith("[未验证：需要处理]")


def test_forbidden_file_unchanged_condition_is_verifiable(tmp_path):
    protected = tmp_path / "protected.txt"
    protected.write_text("original", encoding="utf-8")
    verifier = FileStateVerifier(protected, must_remain_unchanged=True)
    protected.write_text("changed", encoding="utf-8")

    result = Agent(llm=ScriptedLLM([LLMResponse(content="done")])).run({
        "goal": "do not change protected file",
        "verifier": verifier,
    })

    assert result.status is RunStatus.NEEDS_ACTION
    assert result.evidence[0]["metadata"]["unchanged_check"] is True


def test_configured_test_failure_is_structured_as_needs_action():
    verifier = CoreTestResultVerifier(_exit_command(3))
    result = Agent(
        llm=ScriptedLLM([LLMResponse(content="Tests pass.")]),
        permission=Permission(allow_all=True),
    ).run({"goal": "run tests", "verifier": verifier})

    assert result.status is RunStatus.NEEDS_ACTION
    assert result.verification_status is VerificationStatus.FAILED
    assert result.evidence[0]["kind"] == "test_result"
    assert result.evidence[0]["metadata"]["exit_code"] == 3


def test_configured_test_success_can_verify():
    verifier = CoreTestResultVerifier(_exit_command(0))
    result = Agent(
        llm=ScriptedLLM([LLMResponse(content="Tests pass.")]),
        permission=Permission(allow_all=True),
    ).run({"goal": "run tests", "verifier": verifier})

    assert result.status is RunStatus.VERIFIED
    assert result.verification_status is VerificationStatus.PASSED


def test_verification_command_cannot_bypass_permission_system(tmp_path):
    marker = tmp_path / "must-not-exist.txt"
    executable = sys.executable.replace("\\", "/")
    command = (
        f'"{executable}" -c "from pathlib import Path; '
        f"Path(r'{marker.as_posix()}').write_text('bad')\""
    )
    asked = []
    permission = Permission(ask=lambda name, arguments: asked.append(name) or "deny")

    result = Agent(
        llm=ScriptedLLM([LLMResponse(content="I verified it.")]),
        permission=permission,
    ).run({"goal": "verify", "verifier": CoreTestResultVerifier(command)})

    assert asked == ["bash"]
    assert not marker.exists()
    assert result.status is RunStatus.NEEDS_ACTION
    assert result.evidence[0]["metadata"]["authorized"] is False


def test_required_evidence_missing_stays_unverified(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    result = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("read-1", "read_file", {"file_path": str(target)})]),
            LLMResponse(content="done"),
        ]),
        tools=[ReadFileTool()],
    ).run({
        "goal": "read and prove another action",
        "verifier": RequiredEvidenceVerifier(["tool:missing:write_file"]),
    })

    assert result.status is RunStatus.UNVERIFIED
    assert result.exit_reason is ExitReason.INSUFFICIENT_EVIDENCE
    assert result.verification_status is VerificationStatus.INSUFFICIENT_EVIDENCE
    assert result.evidence[0]["passed"] is False


def test_required_tool_evidence_can_verify(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    result = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("read-1", "read_file", {"file_path": str(target)})]),
            LLMResponse(content="done"),
        ]),
        tools=[ReadFileTool()],
    ).run({
        "goal": "read file",
        "verifier": RequiredEvidenceVerifier(["tool:read-1:read_file"]),
    })

    assert result.status is RunStatus.VERIFIED
    assert result.evidence[0]["passed"] is True


def test_no_failed_tool_call_verifier_rejects_unresolved_failure(tmp_path):
    missing = tmp_path / "missing.txt"
    result = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("read-1", "read_file", {"file_path": str(missing)})]),
            LLMResponse(content="done"),
        ]),
        tools=[ReadFileTool()],
    ).run({"goal": "read missing file", "verifier": NoFailedToolCallVerifier()})

    assert result.status is RunStatus.NEEDS_ACTION
    assert result.evidence[0]["metadata"]["failed_count"] == 1


def test_composite_partial_success_keeps_all_evidence_and_fails_hard_condition(tmp_path):
    present = tmp_path / "present.txt"
    present.write_text("ok", encoding="utf-8")
    verifier = CompositeVerifier([
        FileStateVerifier(present),
        FileStateVerifier(tmp_path / "missing.txt"),
    ])

    result = Agent(llm=ScriptedLLM([LLMResponse(content="all done")])).run({
        "goal": "produce two files",
        "verifier": verifier,
    })

    assert result.status is RunStatus.NEEDS_ACTION
    assert [item["passed"] for item in result.evidence] == [True, False]
    assert len(result.failed_criteria) == 1


def test_no_verifier_chat_marks_model_text_as_unverified():
    agent = Agent(llm=ScriptedLLM([LLMResponse(content="I finished everything.")]))

    assert agent.chat("work") == "[未验证] I finished everything."


def test_legacy_callable_is_adapted_to_evidence_producing_result():
    result = Agent(llm=ScriptedLLM([LLMResponse(content="done")])).run({
        "goal": "legacy",
        "verifier": lambda snapshot: snapshot["status"] == "verifying",
    })

    assert result.status is RunStatus.VERIFIED
    assert result.evidence[0]["kind"] == "trusted_callback"


def test_verification_trace_contains_hashes_not_sensitive_content(tmp_path):
    secret = "SUPER_SECRET_VERIFICATION_VALUE"
    target = tmp_path / "secret.txt"
    target.write_text(secret, encoding="utf-8")
    sink = MemoryTraceSink()
    result = Agent(
        llm=ScriptedLLM([LLMResponse(content="done")]),
        trace_sink=sink,
    ).run({
        "goal": "verify secret file",
        "verifier": FileStateVerifier(target, content_contains=secret),
    })

    assert result.status is RunStatus.VERIFIED
    raw_trace = json.dumps(sink.events, ensure_ascii=False)
    assert secret not in raw_trace
    assert str(target) not in raw_trace
    assert "verification_started" in [event["event_type"] for event in sink.events]
    finished = next(
        event for event in sink.events if event["event_type"] == "verification_finished"
    )
    assert finished["duration_ms"] is not None
    assert finished["details"]["verification_status"] == "passed"
