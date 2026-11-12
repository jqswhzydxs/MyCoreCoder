"""Offline crash/recovery coverage for the durable task journal."""

import sqlite3
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from corecoder import (
    Agent,
    FileStateVerifier,
    RunStatus,
    RunStore,
    RunStoreCorruptError,
    RunStoreVersionError,
    ToolCallStatus,
    checkpoints,
    cli,
    make_idempotency_key,
)
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.run_store import InjectedProcessCrash
from corecoder.tools.base import Tool
from corecoder.tools.edit import EditFileTool
from corecoder.tools.protocol import ToolError
from corecoder.tools.write import WriteFileTool
from tests.conftest import repl_with


class CounterTool(Tool):
    name = "counter"
    description = "Record one externally visible call."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    }
    side_effect = "external"
    risk = "high"
    requires_confirmation = False
    idempotent = False
    parallel_safe = False

    def __init__(self, calls: list[str]):
        self.calls = calls

    def execute(self, label: str) -> str:
        self.calls.append(label)
        return f"recorded {label}"


class SecretTool(Tool):
    name = "secret_tool"
    description = "Exercise persistence redaction."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "payload": {"type": "string"},
            "api_key": {"type": "string"},
        },
        "required": ["payload", "api_key"],
    }
    side_effect = "external"
    risk = "high"
    requires_confirmation = False
    redact_fields = ("payload", "api_key")

    def execute(self, payload: str, api_key: str) -> str:
        return f"accepted {len(payload) + len(api_key)} bytes"


class RetryableTool(Tool):
    name = "retryable"
    description = "Fail once with an explicitly retryable error."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    }
    side_effect = "write"
    risk = "low"
    requires_confirmation = False
    idempotent = True
    retry_policy: ClassVar[dict] = {"max_attempts": 2, "backoff_seconds": 0}

    def __init__(self):
        self.attempts = 0

    def execute(self, label: str) -> str:
        self.attempts += 1
        if self.attempts == 1:
            raise ToolError(
                "Error: temporary offline failure.",
                category="temporary_failure",
                code="TEMPORARY_FAILURE",
                retryable=True,
            )
        return f"recorded {label}"


class CrashOnce:
    def __init__(self, boundary: str):
        self.boundary = boundary
        self.fired = False

    def __call__(self, boundary: str, context: dict) -> None:
        if boundary == self.boundary and not self.fired:
            self.fired = True
            raise InjectedProcessCrash(f"crashed at {boundary} for {context['run_id']}")


def _store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs.sqlite3")


def test_idempotency_key_is_stable_and_step_scoped():
    first = make_idempotency_key("run", "round:1:call:0", "2", {"b": 2, "a": 1})
    reordered = make_idempotency_key("run", "round:1:call:0", "2", {"a": 1, "b": 2})
    another_step = make_idempotency_key("run", "round:1:call:1", "2", {"a": 1, "b": 2})

    assert first == reordered
    assert first != another_step


def test_store_creates_versioned_transactional_schema(tmp_path):
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        schema_version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]

    assert {"runs", "state_versions", "tool_calls", "evidence", "event_refs"} <= tables
    assert schema_version == "1"


def test_model_tool_and_verification_boundaries_are_journaled(tmp_path):
    store = _store(tmp_path)
    calls: list[str] = []
    result = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("counter-1", "counter", {"label": "one"})]),
            LLMResponse(content="done"),
        ]),
        tools=[CounterTool(calls)],
        run_store=store,
    ).run("journal boundaries")

    with sqlite3.connect(store.path) as connection:
        boundaries = {
            row[0]
            for row in connection.execute(
                "SELECT boundary FROM state_versions WHERE run_id=?",
                (result.run_id,),
            )
        }

    assert {
        "before_model",
        "after_model_before_tools",
        "before_tool",
        "after_tool_before_receipt",
        "after_tool_receipt",
        "after_model_before_verification",
        "after_verification",
        "run_finished",
    } <= boundaries


def test_crash_before_non_idempotent_tool_becomes_unknown_and_is_not_replayed(tmp_path):
    calls: list[str] = []
    store = _store(tmp_path)
    tool = CounterTool(calls)
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("counter-1", "counter", {"label": "one"})])
        ]),
        tools=[tool],
        run_store=store,
        crash_injector=CrashOnce("tool_before_execute"),
    )

    with pytest.raises(InjectedProcessCrash):
        agent.run("record once")

    run_id = agent.last_run_id
    assert run_id is not None
    assert calls == []
    assert store.get_tool_calls(run_id)[0].status is ToolCallStatus.STARTED

    resumed = Agent(
        llm=ScriptedLLM([]),
        tools=[tool],
        run_store=store,
    ).resume_run(run_id)

    assert resumed.status is RunStatus.NEEDS_ACTION
    assert resumed.exit_reason.value == "recovery_requires_action"
    assert store.get_tool_calls(run_id)[0].status is ToolCallStatus.UNKNOWN
    status = store.run_status(run_id)
    assert status["recovery_required"][0]["tool_call_id"] == "counter-1"
    assert calls == []


def test_write_success_before_receipt_is_hash_confirmed_and_not_repeated(tmp_path):
    target = tmp_path / "artifact.txt"
    store = _store(tmp_path)
    first = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(
                "write-1",
                "write_file",
                {"file_path": str(target), "content": "durable\n"},
            )])
        ]),
        tools=[WriteFileTool()],
        run_store=store,
        crash_injector=CrashOnce("tool_after_execute_before_receipt"),
    )

    with pytest.raises(InjectedProcessCrash):
        first.run("write artifact")

    run_id = first.last_run_id
    assert run_id is not None
    assert target.read_text(encoding="utf-8") == "durable\n"
    assert checkpoints.pending() == 1
    assert store.get_tool_calls(run_id)[0].status is ToolCallStatus.STARTED

    resumed_agent = Agent(
        llm=ScriptedLLM([LLMResponse(content="artifact is ready")]),
        tools=[WriteFileTool()],
        run_store=store,
    )
    result = resumed_agent.resume_run(run_id)

    assert result.status is RunStatus.UNVERIFIED
    assert store.get_tool_calls(run_id)[0].status is ToolCallStatus.SUCCEEDED
    assert target.read_text(encoding="utf-8") == "durable\n"
    assert checkpoints.pending() == 1

    repeated = Agent(
        llm=ScriptedLLM([]),
        tools=[WriteFileTool()],
        run_store=store,
    ).resume_run(run_id)
    assert repeated.status is RunStatus.UNVERIFIED
    assert checkpoints.pending() == 1


def test_durable_success_is_not_replayed_after_receipt_crash(tmp_path):
    calls: list[str] = []
    store = _store(tmp_path)
    tool = CounterTool(calls)
    first = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("counter-1", "counter", {"label": "one"})])
        ]),
        tools=[tool],
        run_store=store,
        crash_injector=CrashOnce("tool_after_result_persisted_before_receipt"),
    )

    with pytest.raises(InjectedProcessCrash):
        first.run("record exactly once")

    run_id = first.last_run_id
    assert run_id is not None
    assert calls == ["one"]
    assert store.get_tool_calls(run_id)[0].status is ToolCallStatus.SUCCEEDED

    result = Agent(
        llm=ScriptedLLM([LLMResponse(content="done")]),
        tools=[tool],
        run_store=store,
    ).resume_run(run_id)

    assert result.status is RunStatus.UNVERIFIED
    assert calls == ["one"]


def test_retryable_idempotent_failure_follows_tool_policy_on_resume(tmp_path):
    store = _store(tmp_path)
    tool = RetryableTool()
    first = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(
                "retry-1",
                "retryable",
                {"label": "one"},
            )])
        ]),
        tools=[tool],
        run_store=store,
        crash_injector=CrashOnce("tool_after_result_persisted_before_receipt"),
    )

    with pytest.raises(InjectedProcessCrash):
        first.run("retry safely")

    run_id = first.last_run_id
    assert run_id is not None
    failed = store.get_tool_calls(run_id)[0]
    assert failed.status is ToolCallStatus.FAILED
    assert failed.retryable is True

    result = Agent(
        llm=ScriptedLLM([LLMResponse(content="done")]),
        tools=[tool],
        run_store=store,
    ).resume_run(run_id)

    assert result.status is RunStatus.UNVERIFIED
    assert tool.attempts == 2
    assert store.get_tool_calls(run_id)[0].status is ToolCallStatus.SUCCEEDED


def test_edit_success_before_receipt_is_confirmed_by_hash(tmp_path):
    target = tmp_path / "editable.txt"
    target.write_text("before\n", encoding="utf-8")
    store = _store(tmp_path)
    first = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(
                "edit-1",
                "edit_file",
                {
                    "file_path": str(target),
                    "old_string": "before",
                    "new_string": "after",
                },
            )])
        ]),
        tools=[EditFileTool()],
        run_store=store,
        crash_injector=CrashOnce("tool_after_execute_before_receipt"),
    )

    with pytest.raises(InjectedProcessCrash):
        first.run("edit artifact")

    run_id = first.last_run_id
    assert run_id is not None
    assert target.read_text(encoding="utf-8") == "after\n"

    result = Agent(
        llm=ScriptedLLM([LLMResponse(content="edited")]),
        tools=[EditFileTool()],
        run_store=store,
    ).resume_run(run_id)

    assert result.status is RunStatus.UNVERIFIED
    assert store.get_tool_calls(run_id)[0].status is ToolCallStatus.SUCCEEDED
    assert checkpoints.pending() == 1


def test_verification_crash_resumes_without_another_model_call(tmp_path):
    target = tmp_path / "verified.txt"
    target.write_text("ready", encoding="utf-8")
    store = _store(tmp_path)
    verifier = FileStateVerifier(target, content_equals="ready")
    first = Agent(
        llm=ScriptedLLM([LLMResponse(content="done")]),
        tools=[],
        run_store=store,
        crash_injector=CrashOnce("verification_after_execute_before_result"),
    )

    with pytest.raises(InjectedProcessCrash):
        first.run({"goal": "verify artifact", "verifier": verifier})

    run_id = first.last_run_id
    assert run_id is not None
    assert store.load_run(run_id).state["status"] == "verifying"

    result = Agent(
        llm=ScriptedLLM([]),
        tools=[],
        run_store=store,
    ).resume_run(run_id, verifier=verifier)

    assert result.status is RunStatus.VERIFIED
    assert result.evidence

    repeated = Agent(
        llm=ScriptedLLM([]),
        tools=[],
        run_store=store,
    ).resume_run(run_id, verifier=verifier)
    assert repeated.status is RunStatus.VERIFIED


def test_database_never_contains_full_sensitive_parameters(tmp_path):
    secret = "sk-super-secret-value-123456"
    password = "password=hunter2-secret"
    store = _store(tmp_path)
    Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(
                "secret-1",
                "secret_tool",
                {"payload": password, "api_key": secret},
            )]),
            LLMResponse(content="done"),
        ]),
        tools=[SecretTool()],
        run_store=store,
    ).run(f"handle api_key={secret} and {password}")

    database_bytes = store.path.read_bytes()
    wal = Path(f"{store.path}-wal")
    if wal.exists():
        database_bytes += wal.read_bytes()
    assert secret.encode() not in database_bytes
    assert password.encode() not in database_bytes


def test_corrupt_database_fails_safely(tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not a sqlite database")

    with pytest.raises(RunStoreCorruptError, match="corrupt or unreadable"):
        RunStore(path)


def test_incompatible_schema_version_fails_safely(tmp_path):
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE metadata SET value='999' WHERE key='schema_version'"
        )

    with pytest.raises(RunStoreVersionError, match="incompatible"):
        RunStore(store.path)


def test_corrupt_persisted_json_fails_safely(tmp_path):
    store = _store(tmp_path)
    agent = Agent(
        llm=ScriptedLLM([LLMResponse(content="done")]),
        tools=[],
        run_store=store,
    )
    result = agent.run("persist a run")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE runs SET state_json='not-json' WHERE run_id=?",
            (result.run_id,),
        )

    with pytest.raises(RunStoreCorruptError, match="invalid persisted JSON"):
        store.load_run(result.run_id)


def test_cli_accepts_resume_run_and_custom_store(monkeypatch, tmp_path):
    store_path = tmp_path / "custom.sqlite3"
    monkeypatch.setattr(
        sys,
        "argv",
        ["corecoder", "--resume-run", "run-123", "--run-store", str(store_path)],
    )

    args = cli._parse_args()

    assert args.resume_run == "run-123"
    assert args.run_store == str(store_path)


def test_runs_and_run_status_slash_commands_are_offline(tmp_path, monkeypatch, capsys):
    store = _store(tmp_path)
    agent = Agent(
        llm=ScriptedLLM([LLMResponse(content="done")]),
        tools=[],
        run_store=store,
    )
    result = agent.run("persist this task")

    repl_with(monkeypatch, agent, ["/runs", "/run-status", "quit"])

    output = capsys.readouterr().out
    assert result.run_id in output
    assert "persist this task" in output
    assert "unverified" in output
