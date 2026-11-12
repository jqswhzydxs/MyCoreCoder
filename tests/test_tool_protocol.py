"""Structured ToolSpec/ToolResult protocol and legacy compatibility."""

import json
import threading
import time
from typing import ClassVar

from corecoder import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.mcp import MCPTool
from corecoder.permissions import Permission
from corecoder.tools import ALL_TOOLS
from corecoder.tools.base import Tool
from corecoder.tools.protocol import (
    LegacyToolAdapter,
    ToolError,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from corecoder.tools.write import WriteFileTool
from corecoder.trace import MemoryTraceSink


class _LegacyEcho(Tool):
    name = "legacy_echo"
    description = "Return a legacy string."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    side_effect = "none"
    risk = "low"
    required_permissions: ClassVar[tuple[str, ...]] = ()
    requires_confirmation = False
    idempotent = True
    parallel_safe = True

    def execute(self, text: str) -> str:
        return f"echo: {text}"


def test_function_schema_excludes_runtime_governance_metadata():
    tool = _LegacyEcho()
    spec = tool.spec

    assert isinstance(spec, ToolSpec)
    assert set(tool.schema()["function"]) == {"name", "description", "parameters"}
    assert "risk" not in json.dumps(tool.schema())
    assert spec.runtime_metadata()["parallel_safe"] is True
    assert spec.runtime_metadata()["side_effect"] == "none"


def test_all_builtin_tools_publish_complete_governance_metadata():
    for tool in ALL_TOOLS:
        spec = tool.spec
        assert spec.side_effect in {"none", "read", "write", "external"}
        assert spec.risk in {"low", "medium", "high"}
        assert isinstance(spec.required_permissions, tuple)
        assert isinstance(spec.requires_confirmation, bool)
        assert isinstance(spec.idempotent, bool)
        assert isinstance(spec.parallel_safe, bool)
        assert spec.timeout_seconds is None or spec.timeout_seconds > 0
        assert spec.version


def test_legacy_string_tool_is_adapted_to_tool_result():
    result = LegacyToolAdapter(_LegacyEcho()).invoke("call-1", {"text": "hello"})

    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.outcome is ToolOutcome.SUCCESS
    assert result.data == "echo: hello"
    assert result.model_content() == "echo: hello"
    assert result.evidence == ("tool:call-1:legacy_echo",)


class _SchemaChecked(Tool):
    name = "schema_checked"
    description = "Validate an integer before execution."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    }
    executed = False

    def execute(self, count: int) -> str:
        self.executed = True
        return str(count)


def test_input_schema_rejects_wrong_type_before_execution():
    tool = _SchemaChecked()
    result = LegacyToolAdapter(tool).invoke("call-1", {"count": "secret-value"})

    assert result.outcome is ToolOutcome.VALIDATION_ERROR
    assert result.error_category == "invalid_arguments"
    assert "secret-value" not in result.model_content()
    assert tool.executed is False


class _SignatureMismatch(Tool):
    name = "signature_mismatch"
    description = "Schema and Python signature intentionally disagree."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }

    def execute(self, value: int) -> str:
        return str(value)


def test_python_signature_is_checked_after_schema():
    result = LegacyToolAdapter(_SignatureMismatch()).invoke("call-2", {"count": 1})

    assert result.outcome is ToolOutcome.VALIDATION_ERROR
    assert "signature validation failed" in result.model_content()


class _BusinessFailure(Tool):
    name = "business_failure"
    description = "Raise a safe domain error."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}, "required": []}

    def execute(self) -> str:
        raise ToolError(
            "Error: quota is exhausted.",
            category="quota",
            code="QUOTA_EXHAUSTED",
            retryable=True,
            debug_message="provider account 123 exhausted its private quota",
        )


def test_business_error_preserves_only_declared_safe_message():
    result = LegacyToolAdapter(_BusinessFailure()).invoke("call-3", {})

    assert result.outcome is ToolOutcome.BUSINESS_ERROR
    assert result.error_category == "quota"
    assert result.error_code == "QUOTA_EXHAUSTED"
    assert result.retryable is True
    assert result.model_content() == "Error: quota is exhausted."
    assert "provider account" not in result.model_content()


class _SlowTool(Tool):
    name = "slow"
    description = "Sleep past the runtime timeout."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}, "required": []}
    timeout_seconds = 0.01

    def execute(self) -> str:
        time.sleep(0.1)
        return "late"


def test_runtime_timeout_becomes_retryable_tool_result():
    result = LegacyToolAdapter(_SlowTool()).invoke("call-4", {})

    assert result.outcome is ToolOutcome.TIMEOUT
    assert result.error_code == "TOOL_TIMEOUT"
    assert result.retryable is True
    assert "timed out" in result.model_content()


class _UnknownFailure(Tool):
    name = "unknown_failure"
    description = "Raise an unexpected exception containing sensitive text."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}, "required": []}
    side_effect = "none"
    risk = "low"
    required_permissions: ClassVar[tuple[str, ...]] = ()
    requires_confirmation = False

    def execute(self) -> str:
        raise RuntimeError("authorization=Bearer private-token password=hunter2")


def test_unknown_exception_is_safe_for_model_and_controlled_in_trace():
    sink = MemoryTraceSink()
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(
                id="unknown-1",
                name="unknown_failure",
                arguments={},
            )]),
            LLMResponse(content="recovered"),
        ]),
        tools=[_UnknownFailure()],
        trace_sink=sink,
    )

    assert agent.chat("run it") == "[未验证] recovered"
    model_error = agent.messages[2]["content"]
    assert "failed unexpectedly" in model_error
    assert "private-token" not in model_error
    assert "hunter2" not in json.dumps(sink.events)
    failed = next(event for event in sink.events if event["event_type"] == "tool_failed")
    assert failed["error_category"] == "tool_exception"
    assert failed["details"]["error_code"] == "UNEXPECTED_RUNTIMEERROR"
    assert agent.tool_results[0].outcome is ToolOutcome.UNKNOWN_ERROR


def test_permission_uses_toolspec_instead_of_hardcoded_tool_name():
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(
                id="echo-1",
                name="legacy_echo",
                arguments={"text": "allowed"},
            )]),
            LLMResponse(content="done"),
        ]),
        tools=[_LegacyEcho()],
        permission=Permission(),
    )

    assert agent.chat("go") == "[未验证] done"
    assert agent.tool_results[0].outcome is ToolOutcome.SUCCESS


def test_permission_denial_is_also_a_structured_tool_result(tmp_path):
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(
                id="write-1",
                name="write_file",
                arguments={"file_path": str(tmp_path / "no.txt"), "content": "secret"},
            )]),
            LLMResponse(content="denied safely"),
        ]),
        tools=[WriteFileTool()],
        permission=Permission(),
    )

    assert agent.chat("go") == "[未验证] denied safely"
    result = agent.tool_results[0]
    assert result.outcome is ToolOutcome.PERMISSION_DENIED
    assert result.error_code == "PERMISSION_DENIED"
    assert result.data is None
    assert not (tmp_path / "no.txt").exists()


class _SerialProbe(Tool):
    name = "serial_probe"
    description = "Detect overlapping calls."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
    }
    side_effect = "write"
    risk = "medium"
    required_permissions: ClassVar[tuple[str, ...]] = ()
    requires_confirmation = False
    parallel_safe = False

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def execute(self, value: int) -> str:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.02)
        with self.lock:
            self.active -= 1
        return str(value)


def test_runtime_serializes_tools_marked_not_parallel_safe():
    tool = _SerialProbe()
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[
                ToolCall(id="serial-1", name=tool.name, arguments={"value": 1}),
                ToolCall(id="serial-2", name=tool.name, arguments={"value": 2}),
            ]),
            LLMResponse(content="done"),
        ]),
        tools=[tool],
    )

    assert agent.chat("go") == "[未验证] done"
    assert tool.max_active == 1
    assert [result.data for result in agent.tool_results] == ["1", "2"]


class _FakeMCPClient:
    name = "fake"

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        return f"{tool_name}: {arguments}"


def test_mcp_tool_defaults_to_external_high_risk_and_serial():
    tool = MCPTool(
        _FakeMCPClient(),
        {
            "name": "remote",
            "description": "remote tool",
            "inputSchema": {"type": "object", "properties": {}},
        },
    )

    assert tool.spec.side_effect == "external"
    assert tool.spec.risk == "high"
    assert tool.spec.parallel_safe is False
    assert tool.spec.requires_confirmation is True
    assert tool.spec.required_permissions == ("external:unknown-side-effect",)


def test_mcp_metadata_only_relaxes_defaults_when_explicitly_trusted():
    tool = MCPTool(
        _FakeMCPClient(),
        {
            "name": "remote_read",
            "description": "trusted remote reader",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
            "_meta": {"corecoder/toolSpec": {"parallel_safe": True, "version": "2"}},
        },
        trust_metadata=True,
    )

    assert tool.spec.side_effect == "read"
    assert tool.spec.risk == "low"
    assert tool.spec.requires_confirmation is False
    assert tool.spec.parallel_safe is True
    assert tool.spec.idempotent is True
    assert tool.spec.version == "2"
