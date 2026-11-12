"""Offline coverage for optional, metadata-only runtime tracing."""

import json
from typing import ClassVar

from corecoder import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.tools.base import Tool
from corecoder.tools.read import ReadFileTool
from corecoder.tools.write import WriteFileTool
from corecoder.trace import JsonlTraceSink, MemoryTraceSink, TraceEvent
from corecoder.trace_replay import load_trace, render, summarize
from tests.conftest import repl_with


def _event_types(sink):
    return [event["event_type"] for event in sink.events]


def test_trace_records_complete_normal_run_without_changing_return_value(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("private file payload", encoding="utf-8")
    sink = MemoryTraceSink()
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(
                tool_calls=[ToolCall(
                    id="read-1",
                    name="read_file",
                    arguments={"file_path": str(target)},
                )],
                prompt_tokens=11,
                completion_tokens=3,
            ),
            LLMResponse(content="done", prompt_tokens=7, completion_tokens=1),
        ]),
        tools=[ReadFileTool()],
        trace_sink=sink,
    )

    assert agent.chat("read it") == "[未验证] done"
    assert [
        event["event_type"]
        for event in sink.events
        if event["event_type"] not in {"budget_checked", "state_transition"}
    ] == [
        "run_started",
        "model_started",
        "model_finished",
        "tool_planned",
        "schedule_decided",
        "tool_queue_finished",
        "permission_allowed",
        "tool_started",
        "tool_finished",
        "model_started",
        "model_finished",
            "verification_started",
            "verification_finished",
        "run_finished",
    ]
    assert len({event["trace_id"] for event in sink.events}) == 1
    assert len({event["run_id"] for event in sink.events}) == 1
    assert [event["step_id"] for event in sink.events] == list(range(1, len(sink.events) + 1))
    assert sink.events[-1]["outcome"] == "model_stopped_unverified"
    assert sink.events[-1]["details"]["budget_usage"]["rounds"] == 2
    assert sink.events[-1]["details"]["cost_unavailable"] is True
    transitions = [
        event for event in sink.events if event["event_type"] == "state_transition"
    ]
    assert [
        (event["details"]["from_status"], event["details"]["to_status"])
        for event in transitions
    ] == [
        ("pending", "running"),
        ("running", "waiting_approval"),
        ("waiting_approval", "executing"),
        ("executing", "running"),
        ("running", "verifying"),
        ("verifying", "unverified"),
    ]
    assert all(
        event["details"]["after_state_version"]
        == event["details"]["before_state_version"] + 1
        for event in transitions
    )
    assert all("private file payload" not in json.dumps(event) for event in sink.events)


class _BoomTool(Tool):
    name = "boom"
    description = "Raise an exception for trace testing."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}, "required": []}

    def execute(self) -> str:
        raise RuntimeError("secret exception detail")


def test_trace_classifies_tool_exception_without_recording_error_text():
    sink = MemoryTraceSink()
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(id="boom-1", name="boom", arguments={})]),
            LLMResponse(content="recovered"),
        ]),
        tools=[_BoomTool()],
        trace_sink=sink,
    )

    assert agent.chat("go") == "[未验证] recovered"
    failed = next(event for event in sink.events if event["event_type"] == "tool_failed")
    assert failed["tool_call_id"] == "boom-1"
    assert failed["error_category"] == "tool_exception"
    assert "secret exception detail" not in json.dumps(sink.events)


def test_trace_records_permission_denial_without_starting_tool(tmp_path):
    sink = MemoryTraceSink()
    target = tmp_path / "denied.txt"
    call = ToolCall(
        id="write-1",
        name="write_file",
        arguments={"file_path": str(target), "content": "must not be traced or written"},
    )
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[call]),
            LLMResponse(content="understood"),
        ]),
        tools=[WriteFileTool()],
        permission=Permission(),
        trace_sink=sink,
    )

    assert agent.chat("write") == "[未验证] understood"
    assert not target.exists()
    assert "permission_denied" in _event_types(sink)
    assert "tool_started" not in _event_types(sink)
    denied = next(event for event in sink.events if event["event_type"] == "permission_denied")
    assert denied["error_category"] == "permission_denied"
    assert "must not be traced or written" not in json.dumps(sink.events)


def test_trace_covers_each_parallel_tool_call(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    calls = [
        ToolCall(id="read-a", name="read_file", arguments={"file_path": str(first)}),
        ToolCall(id="read-b", name="read_file", arguments={"file_path": str(second)}),
    ]
    sink = MemoryTraceSink()
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=calls),
            LLMResponse(content="both read"),
        ]),
        tools=[ReadFileTool()],
        trace_sink=sink,
    )

    assert agent.chat("read both") == "[未验证] both read"
    for event_type in (
        "tool_planned",
        "tool_queue_finished",
        "permission_allowed",
        "tool_started",
        "tool_finished",
        "tool_execution_finished",
    ):
        ids = {
            event["tool_call_id"]
            for event in sink.events
            if event["event_type"] == event_type
        }
        assert ids == {"read-a", "read-b"}


def test_jsonl_sink_redacts_sensitive_fields(tmp_path):
    path = tmp_path / "trace.jsonl"
    sink = JsonlTraceSink(path)
    sink.emit(TraceEvent(
        trace_id="trace-1",
        run_id="run-1",
        step_id=1,
        event_type="run_started",
        started_at="2026-01-01T00:00:00.000Z",
        details={
            "access_token": "token-value",
            "api_key": "key-value",
            "headers": {"Authorization": "Bearer auth-value"},
            "password": "password-value",
            "client_secret": "secret-value",
            "reasoning_content": "hidden chain",
            "system_prompt": "full private prompt",
            "file_content": "unredacted file content",
            "safe": "kept",
        },
    ))

    raw = path.read_text(encoding="utf-8")
    for secret in (
        "token-value",
        "key-value",
        "auth-value",
        "password-value",
        "secret-value",
        "hidden chain",
        "full private prompt",
        "unredacted file content",
    ):
        assert secret not in raw
    event = json.loads(raw)
    assert event["details"]["safe"] == "kept"
    assert event["details"]["api_key"] == "[REDACTED]"
    assert event["details"]["headers"]["Authorization"] == "[REDACTED]"


class _FailingSink:
    enabled = True

    def emit(self, event):
        raise OSError("disk unavailable")

    def status(self):
        return "enabled (broken test sink)"


def test_trace_sink_failure_never_fails_agent_task():
    agent = Agent(
        llm=ScriptedLLM([LLMResponse(content="still succeeds")]),
        trace_sink=_FailingSink(),
    )

    assert agent.chat("hello") == "[未验证] still succeeds"


def test_trace_records_context_compression(monkeypatch):
    sink = MemoryTraceSink()
    agent = Agent(
        llm=ScriptedLLM([LLMResponse(content="done")]),
        trace_sink=sink,
    )
    monkeypatch.setattr(agent.context, "maybe_compress", lambda messages, llm: True)

    assert agent.chat("compress") == "[未验证] done"
    compressed = next(
        event for event in sink.events if event["event_type"] == "context_compressed"
    )
    assert compressed["outcome"] == "compressed"
    assert compressed["details"]["before_messages"] == 1
    assert compressed["details"]["after_messages"] == 1


def test_replay_reports_path_slowest_steps_exit_and_latency(tmp_path):
    path = tmp_path / "trace.jsonl"
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(
                id="read-1",
                name="read_file",
                arguments={"file_path": str(target)},
            )]),
            LLMResponse(content="done"),
        ]),
        tools=[ReadFileTool()],
        trace_sink=JsonlTraceSink(path),
    )
    agent.chat("read")

    summaries = summarize(load_trace(path))
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["exit_reason"] == "model_stopped_unverified"
    assert summary["total_duration_ms"] is not None
    assert summary["model_duration_ms"] >= 0
    assert summary["tool_duration_ms"] >= 0
    assert summary["slowest_steps"]
    output = render(summaries)
    assert "Key path:" in output
    assert "Slowest steps:" in output
    assert "Exit: model_stopped_unverified" in output


def test_trace_cli_flag_and_status_command(monkeypatch, capsys, tmp_path):
    from corecoder.cli import _parse_args

    trace_path = tmp_path / "events.jsonl"
    monkeypatch.setattr("sys.argv", ["corecoder", "--trace-file", str(trace_path)])
    assert _parse_args().trace_file == str(trace_path)

    agent = Agent(llm=ScriptedLLM([]), trace_sink=MemoryTraceSink())
    repl_with(monkeypatch, agent, ["/trace", "quit"])
    assert "Trace: enabled (memory, 0 events)" in capsys.readouterr().out


def test_default_trace_is_disabled_and_retains_nothing():
    agent = Agent(llm=ScriptedLLM([LLMResponse(content="same result")]))

    assert agent.trace.status() == "disabled"
    assert agent.chat("hello") == "[未验证] same result"
