"""Deterministic coverage for streamed Bash output and cancellation."""

import json
import sys
import threading
import time
from typing import ClassVar

from corecoder import Agent
from corecoder.demo import ScriptedLLM
from corecoder.events import AgentEventType
from corecoder.llm import LLMResponse, ToolCall
from corecoder.tools import bash as bash_module
from corecoder.tools.base import Tool
from corecoder.tools.bash import BashTool
from corecoder.tools.protocol import ToolExecutionContext, ToolOutcome, ToolResult


class MutableProbe:
    def __init__(self) -> None:
        self.cancelled = False

    def __call__(self) -> bool:
        return self.cancelled


def python_command(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def make_context(probe=None, sink=None, timeout=10.0):
    return ToolExecutionContext(
        run_id="run-test",
        tool_call_id="call-test",
        tool_name="bash",
        deadline=time.monotonic() + timeout,
        cancellation_probe=probe,
        output_sink=sink,
    )


def test_stdout_chunk_is_visible_before_process_exit():
    seen: list[tuple[float, str, str]] = []
    started = time.monotonic()

    def sink(channel, text):
        seen.append((time.monotonic(), channel, text))

    code = "import time; print('early-line', flush=True); time.sleep(1.2); print('late-line')"
    result = BashTool().execute_with_context(
        {"command": python_command(code), "timeout": 10},
        make_context(sink=sink),
    )

    assert isinstance(result, str)
    assert "early-line" in result and "late-line" in result
    early = next(item for item in seen if "early-line" in item[2])
    assert early[0] - started < 1.0


def test_stderr_chunk_is_visible_before_process_exit():
    seen: list[tuple[str, str]] = []

    def sink(channel, text):
        seen.append((channel, text))

    code = (
        "import sys, time; print('stdout-now', flush=True); "
        "print('stderr-now', file=sys.stderr, flush=True); time.sleep(0.8)"
    )
    result = BashTool().execute_with_context(
        {"command": python_command(code), "timeout": 10},
        make_context(sink=sink),
    )

    assert isinstance(result, str)
    assert "stdout-now" in result
    assert "stderr-now" in result
    assert any(channel == "stderr" and "stderr-now" in text for channel, text in seen)


def test_unicode_and_unterminated_output_are_preserved():
    seen: list[str] = []
    code = "import sys; sys.stdout.write('你好，世界'); sys.stdout.flush()"
    result = BashTool().execute_with_context(
        {"command": python_command(code), "timeout": 10},
        make_context(sink=lambda _channel, text: seen.append(text)),
    )

    assert isinstance(result, str)
    assert "你好，世界" in result
    assert any("你好，世界" in chunk for chunk in seen)


def test_nonzero_exit_still_uses_legacy_exit_code_marker():
    result = BashTool().execute_with_context(
        {"command": python_command("import sys; sys.exit(42)"), "timeout": 10},
        make_context(),
    )
    assert isinstance(result, str)
    assert "[exit code: 42]" in result


def test_timeout_returns_structured_failure_with_partial_output():
    code = "import time; print('before-timeout', flush=True); time.sleep(5)"
    started = time.monotonic()
    result = BashTool().execute_with_context(
        {"command": python_command(code), "timeout": 0.4},
        make_context(timeout=0.4),
    )

    assert isinstance(result, ToolResult)
    assert result.outcome is ToolOutcome.TIMEOUT
    assert not result.ok
    assert "timed out" in result.safe_message
    assert "before-timeout" in result.safe_message
    assert time.monotonic() - started < 3.0


def test_cancellation_stops_running_process():
    probe = MutableProbe()
    code = "import time; print('started', flush=True); time.sleep(5)"
    timer = threading.Timer(0.5, lambda: setattr(probe, "cancelled", True))
    timer.start()
    started = time.monotonic()
    try:
        result = BashTool().execute_with_context(
            {"command": python_command(code), "timeout": 10},
            make_context(probe=probe),
        )
    finally:
        timer.cancel()

    assert isinstance(result, ToolResult)
    assert result.outcome is ToolOutcome.CANCELLED
    assert "cancelled" in result.safe_message
    assert time.monotonic() - started < 3.0


def test_long_output_is_head_and_tail_truncated():
    code = "import sys; sys.stdout.write('x' * 20000); sys.stdout.flush()"
    result = BashTool().execute_with_context(
        {"command": python_command(code), "timeout": 10},
        make_context(),
    )

    assert isinstance(result, str)
    assert "truncated" in result
    assert len(result) < 16_000
    assert result.startswith("x")
    assert result.rstrip().endswith("x")


def test_output_events_precede_tool_finished_and_do_not_leak_command():
    marker = "COMMAND_MARKER_SHOULD_NOT_APPEAR"
    command = python_command("print('stream-ok', flush=True)") + " # " + marker
    events = []
    result = Agent(
        llm=ScriptedLLM([
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call-1", name="bash", arguments={"command": command})],
            ),
            LLMResponse(content="done"),
        ]),
        tools=[BashTool()],
    ).run("run a command", on_event=events.append)

    assert result.final_text == "done"
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)
    started = next(i for i, event in enumerate(events) if event.event_type is AgentEventType.TOOL_STARTED)
    outputs = [i for i, event in enumerate(events) if event.event_type is AgentEventType.TOOL_OUTPUT]
    finished = next(
        i
        for i, event in enumerate(events)
        if event.event_type in {AgentEventType.TOOL_FINISHED, AgentEventType.TOOL_FAILED}
    )
    assert outputs and started < outputs[0] and max(outputs) < finished
    assert any(
        event.public_data.get("text") == "stream-ok"
        or "stream-ok" in str(event.public_data.get("text", ""))
        for event in events
        if event.event_type is AgentEventType.TOOL_OUTPUT
    )
    serialized = json.dumps([event.public_data for event in events], ensure_ascii=False)
    assert marker not in serialized


def test_no_output_returns_placeholder_and_emits_nothing():
    seen: list[str] = []
    result = BashTool().execute_with_context(
        {"command": python_command("pass"), "timeout": 10},
        make_context(sink=lambda _channel, text: seen.append(text)),
    )

    assert result == "(no output)"
    assert seen == []


def test_legacy_execute_contract_is_unchanged():
    tool = BashTool()
    out = tool.execute(command=python_command("print('legacy-ok')"), timeout=10)
    assert isinstance(out, str)
    assert "legacy-ok" in out


def test_windows_taskkill_tree_arguments_are_built(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(bash_module.os, "name", "nt")
    monkeypatch.setattr(bash_module.subprocess, "run", fake_run)
    bash_module._taskkill_tree(1234)

    assert calls
    assert calls[0][0] == ["taskkill", "/PID", "1234", "/T", "/F"]


def test_adapter_keeps_context_out_of_model_schema():
    from corecoder.tools.protocol import LegacyToolAdapter

    adapter = LegacyToolAdapter(BashTool())
    schema = adapter.spec.function_schema()
    assert "context" not in json.dumps(schema)
    assert set(schema["function"]["parameters"]["properties"]) == {"command", "timeout"}


class _NoopTool(Tool):
    name = "noop"
    description = "noop"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}
    side_effect = "none"
    idempotent = True

    def execute(self):
        return "ok"


def test_legacy_tools_without_context_keep_working():
    from corecoder.tools.protocol import LegacyToolAdapter

    result = LegacyToolAdapter(_NoopTool()).invoke("call-noop", {})
    assert result.ok
    assert result.data == "ok"
