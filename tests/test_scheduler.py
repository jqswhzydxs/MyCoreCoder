"""Deterministic offline tests for metadata-driven tool scheduling."""

import os
import threading
from typing import ClassVar

import pytest

from corecoder.llm import ToolCall
from corecoder.mcp import MCPTool
from corecoder.scheduler import SchedulerInterrupted, ToolScheduler
from corecoder.tools.base import Tool
from corecoder.tools.bash import BashTool
from corecoder.tools.edit import EditFileTool
from corecoder.tools.glob_tool import GlobTool
from corecoder.tools.grep import GrepTool
from corecoder.tools.protocol import ToolResult
from corecoder.tools.read import ReadFileTool
from corecoder.tools.write import WriteFileTool
from corecoder.trace import MemoryTraceSink, TraceRecorder


class _ReadProbe(Tool):
    name = "read_probe"
    description = "Safe read scheduling probe."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "value": {"type": "integer"},
            "resource": {"type": "string"},
        },
        "required": ["value", "resource"],
    }
    side_effect = "read"
    risk = "low"
    required_permissions: ClassVar[tuple[str, ...]] = ()
    requires_confirmation = False
    idempotent = True
    parallel_safe = True
    timeout_seconds = None

    def execute(self, value: int, resource: str) -> str:
        return str(value)

    def resource_keys(self, arguments: dict) -> tuple[str, ...]:
        return (arguments["resource"],)


class _WriteProbe(_ReadProbe):
    name = "write_probe"
    side_effect = "write"
    risk = "medium"
    idempotent = False


class _ApprovalProbe(_ReadProbe):
    name = "approval_probe"
    requires_confirmation = True


def _calls(name="read_probe", *, same_resource=False):
    return [
        ToolCall(
            id=f"call-{value}",
            name=name,
            arguments={
                "value": value,
                "resource": "shared" if same_resource else f"resource-{value}",
            },
        )
        for value in (1, 2)
    ]


def _success(call):
    return ToolResult.success(call.id, call.name, call.arguments["value"])


def test_safe_reads_really_overlap_and_results_keep_model_order():
    """A barrier proves overlap without depending on wall-clock thresholds."""
    scheduler = ToolScheduler([_ReadProbe()], max_workers=2)
    barrier = threading.Barrier(2, timeout=1)
    lock = threading.Lock()
    active = 0
    max_active = 0

    def invoke(call):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        barrier.wait()
        with lock:
            active -= 1
        # Reverse completion order while preserving returned order.
        if call.id == "call-1":
            threading.Event().wait(0.02)
        return _success(call)

    results = scheduler.execute(_calls(), invoke)

    assert max_active == 2
    assert [result.call_id for result in results] == ["call-1", "call-2"]
    assert [result.data for result in results] == [1, 2]


def test_read_read_resource_sharing_is_not_a_conflict():
    scheduler = ToolScheduler([_ReadProbe()])

    plan = scheduler.plan(_calls(same_resource=True))

    assert plan.parallel is True
    assert plan.conflict_count == 0


def test_builtin_readers_can_parallel_and_file_paths_are_canonical(tmp_path):
    target = tmp_path / "nested" / ".." / "note.txt"
    tools = [ReadFileTool(), GrepTool(), GlobTool()]
    scheduler = ToolScheduler(tools)
    calls = [
        ToolCall("read", "read_file", {"file_path": str(target)}),
        ToolCall("grep", "grep", {"pattern": "x", "path": str(tmp_path)}),
        ToolCall("glob", "glob", {"pattern": "*.txt", "path": str(tmp_path)}),
    ]

    plan = scheduler.plan(calls)

    expected = f"file:{os.path.normcase(str(target.resolve()))}"
    assert plan.parallel is True
    assert plan.resource_keys[0] == (expected,)


def test_same_file_edit_and_write_have_a_resource_conflict(tmp_path):
    scheduler = ToolScheduler([EditFileTool(), WriteFileTool()])
    target = str(tmp_path / "same.txt")
    calls = [
        ToolCall(
            "edit",
            "edit_file",
            {"file_path": target, "old_string": "a", "new_string": "b"},
        ),
        ToolCall("write", "write_file", {"file_path": target, "content": "c"}),
    ]

    plan = scheduler.plan(calls)

    assert plan.parallel is False
    assert plan.conflict_count == 1
    assert plan.resource_keys[0] == plan.resource_keys[1]


def test_bash_and_mcp_publish_conservative_resources():
    class Client:
        name = "server"

        def call_tool(self, tool_name, arguments):
            return "ok"

    mcp = MCPTool(
        Client(),
        {"name": "remote", "inputSchema": {"type": "object", "properties": {}}},
    )

    assert BashTool().resource_keys({"command": "pwd"}) == ("shell:global-cwd",)
    assert mcp.resource_keys({}) == ("mcp:server:remote",)


@pytest.mark.parametrize(
    ("tool", "reason"),
    [
        (_WriteProbe(), "side_effect_write"),
        (_ApprovalProbe(), "interactive_approval"),
    ],
)
def test_side_effects_and_interactive_approval_force_serial(tool, reason):
    scheduler = ToolScheduler([tool])
    plan = scheduler.plan(_calls(tool.name, same_resource=True))
    active = 0
    max_active = 0

    def invoke(call):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        active -= 1
        return _success(call)

    results = scheduler.execute(plan.tool_calls, invoke, plan=plan)

    assert plan.parallel is False
    assert any(item.startswith(reason) for item in plan.reasons)
    assert max_active == 1
    assert [result.data for result in results] == [1, 2]


def test_unresolved_dependency_forces_serial_until_resolved():
    scheduler = ToolScheduler([_ReadProbe()])
    calls = _calls()
    calls[1].depends_on = (calls[0].id,)

    blocked = scheduler.plan(calls)
    resolved = scheduler.plan(calls, resolved_call_ids={calls[0].id})

    assert blocked.parallel is False
    assert any(reason.startswith("unresolved_dependencies") for reason in blocked.reasons)
    assert resolved.parallel is True


def test_one_parallel_exception_does_not_drop_sibling_results():
    scheduler = ToolScheduler([_ReadProbe()])

    def invoke(call):
        if call.id == "call-1":
            raise ValueError("private failure detail")
        return _success(call)

    results = scheduler.execute(_calls(), invoke)

    assert [result.call_id for result in results] == ["call-1", "call-2"]
    assert results[0].ok is False
    assert results[0].error_category == "scheduler_execution_error"
    assert "private failure detail" not in results[0].model_content()
    assert results[1].ok is True


def test_interrupt_backfills_every_pending_call_with_legal_results():
    scheduler = ToolScheduler([_ReadProbe()])

    def invoke(call):
        if call.id == "call-1":
            raise KeyboardInterrupt
        return _success(call)

    with pytest.raises(SchedulerInterrupted) as raised:
        scheduler.execute(_calls(), invoke)

    assert isinstance(raised.value.cause, KeyboardInterrupt)
    assert [result.call_id for result in raised.value.results] == ["call-1", "call-2"]
    assert all(isinstance(result, ToolResult) for result in raised.value.results)
    assert raised.value.results[0].error_category == "scheduler_cancelled"
    assert raised.value.results[1].ok is True


def test_cancellation_probe_backfills_calls_before_any_side_effect_starts():
    scheduler = ToolScheduler([_ReadProbe()])
    invoked = []

    with pytest.raises(SchedulerInterrupted) as raised:
        scheduler.execute(
            _calls(),
            lambda call: invoked.append(call.id) or _success(call),
            cancellation_probe=lambda: True,
        )

    assert invoked == []
    assert all(result.error_category == "scheduler_cancelled" for result in raised.value.results)


def test_trace_records_decision_reason_and_queue_wait():
    sink = MemoryTraceSink()
    trace_run = TraceRecorder(sink).new_run("scripted")
    scheduler = ToolScheduler([_WriteProbe()])
    calls = _calls("write_probe", same_resource=True)

    scheduler.execute(calls, _success, trace_run=trace_run, round_no=1)

    decision = next(event for event in sink.events if event["event_type"] == "schedule_decided")
    queued = [event for event in sink.events if event["event_type"] == "tool_queue_finished"]
    executed = [
        event for event in sink.events if event["event_type"] == "tool_execution_finished"
    ]
    assert decision["outcome"] == "serial"
    assert decision["details"]["reason_counts"]["side_effect_write"] == 1
    assert len(queued) == 2
    assert all(event["duration_ms"] is not None for event in queued)
    assert all(event["details"]["schedule_mode"] == "serial" for event in queued)
    assert len(executed) == 2
    assert all(event["duration_ms"] is not None for event in executed)
