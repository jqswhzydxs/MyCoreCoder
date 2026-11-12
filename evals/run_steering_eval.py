"""Deterministic offline evaluation for the steerable runtime.

The scenarios deliberately drive the real Agent loop, scheduler, run store and
event emitter.  No model API key is needed and no metric is hand-written: every
number is derived from the events and messages produced by these runs.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from corecoder import (
    Agent,
    AgentEventType,
    PermissionRequestBroker,
    RunState,
    RunStatus,
    RunStore,
    SteeringMode,
)
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.tools import BashTool, WriteFileTool
from corecoder.tools.base import Tool
from corecoder.trace import MemoryTraceSink

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "evals" / "cases" / "steerable_runtime.json"
RESULTS_DIR = ROOT / "evals" / "results"
SENSITIVE_MARKER = "TOP_SECRET_STEERING_VALUE"


class GateLLM(ScriptedLLM):
    """ScriptedLLM with an explicit gate before selected model calls."""

    def __init__(self, script, *, gate_calls=(0,), model="steering-eval-llm"):
        super().__init__(script, model=model)
        self.gate_calls = set(gate_calls)
        self.entered = threading.Event()
        self.release = threading.Event()
        self._call_index = 0

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None,
             on_stream_activity=None):
        index = self._call_index
        self._call_index += 1
        if index in self.gate_calls:
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("eval gate timed out")
        return super().chat(
            messages,
            tools=tools,
            on_token=on_token,
            on_reasoning=on_reasoning,
            on_stream_activity=on_stream_activity,
        )


class ExecutionProbe(Tool):
    name = "probe"
    description = "Record a deterministic execution."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    }
    side_effect = "none"
    risk = "low"
    idempotent = True
    parallel_safe = True
    requires_confirmation = False

    def __init__(self, executions: list[str]):
        self.executions = executions

    def execute(self, label: str) -> str:
        self.executions.append(label)
        return f"completed {label}"


class ReadProbe(Tool):
    name = "read_probe"
    description = "A deterministic read-only tool."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    side_effect = "read"
    risk = "low"
    idempotent = True
    parallel_safe = True
    requires_confirmation = False
    timeout_seconds = 5

    def __init__(self, executions: list[str], delay: float = 0.0):
        self.executions = executions
        self.delay = delay

    def execute(self, path: str) -> str:
        self.executions.append(path)
        if self.delay:
            time.sleep(self.delay)
        return f"content:{path}"


class SlowWriteProbe(Tool):
    name = "slow_write"
    description = "A deliberately slow write used to steer between serial tools."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }
    side_effect = "write"
    idempotent = False
    parallel_safe = False
    requires_confirmation = False
    timeout_seconds = 5

    def __init__(self):
        self.writes: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(self, path: str, content: str) -> str:
        if not self.entered.is_set():
            self.entered.set()
            self.release.wait(3)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.writes.append(str(target))
        return f"wrote {target}"


class ConfirmProbe(Tool):
    name = "confirm_probe"
    description = "A write-like tool that needs explicit confirmation."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    }
    side_effect = "write"
    idempotent = True
    parallel_safe = False
    requires_confirmation = True

    def __init__(self, executions: list[str]):
        self.executions = executions

    def execute(self, label: str) -> str:
        self.executions.append(label)
        return f"confirmed {label}"


class ExplodingSink:
    def emit(self, event) -> None:
        raise RuntimeError(f"caller sink failed for {event.event_type.value}")


@dataclass
class ScenarioResult:
    scenario: str
    result: Any = None
    events: list[tuple[float, Any]] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    trace_events: list[dict] = field(default_factory=list)
    executions: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    error: str | None = None
    timed_out: bool = False
    used_event_stream: bool = True
    payload_leaks: list[str] = field(default_factory=list)

    @property
    def terminal_seen(self) -> bool:
        return bool(self.events) and self.events[-1][1].event_type in {
            AgentEventType.RUN_FINISHED,
            AgentEventType.RUN_FAILED,
            AgentEventType.RUN_CANCELLED,
        }


def _collect(handle, slow: float = 0.0):
    events: list[tuple[float, Any]] = []

    def consume():
        for event in handle.events():
            events.append((time.monotonic(), event))
            if slow:
                time.sleep(slow)

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    return events, thread


def _finish(record: ScenarioResult, handle, thread, timeout: float = 5.0):
    try:
        record.result = handle.result(timeout=timeout)
    except TimeoutError:
        record.timed_out = True
        handle.cancel("eval timeout")
        try:
            record.result = handle.result(timeout=2)
        except Exception:  # noqa: BLE001, S110 - report timeout, not a second failure
            pass
    except BaseException as exc:  # noqa: BLE001 - evaluation must keep running
        record.error = f"{type(exc).__name__}: {exc}"
    thread.join(timeout=2)
    if not thread.is_alive():
        record.events = list(record.events)
    return record


def _run_scenario(scenario: str, workdir: Path) -> ScenarioResult:
    executor = SCENARIOS[scenario]
    return executor(workdir)


def _text_response(text: str) -> LLMResponse:
    return LLMResponse(content=text)


# ---------------------------------------------------------------------------
# Scenario implementations
# ---------------------------------------------------------------------------


def token_streaming(workdir: Path) -> ScenarioResult:
    agent = Agent(llm=ScriptedLLM([_text_response("alpha beta gamma")]), tools=[])
    handle = agent.start("stream text")
    events, thread = _collect(handle)
    result = handle.result(timeout=3)
    thread.join(1)
    return ScenarioResult("token_streaming", result=result, events=events,
                          messages=agent.messages)


def tool_calls_only_progress(workdir: Path) -> ScenarioResult:
    executions: list[str] = []
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("probe-1", "probe", {"label": "one"})]),
        _text_response("done"),
    ])
    agent = Agent(llm=llm, tools=[ExecutionProbe(executions)])
    handle = agent.start("tool only")
    events, thread = _collect(handle)
    result = handle.result(timeout=3)
    thread.join(1)
    return ScenarioResult("tool_calls_only_progress", result=result, events=events,
                          messages=agent.messages, executions=executions)


def _steer_during_model(workdir: Path, scenario: str, mode: SteeringMode):
    executions: list[str] = []
    tool = ExecutionProbe(executions)
    llm = GateLLM([
        LLMResponse(tool_calls=[
            ToolCall("probe-a", "probe", {"label": "frontend"}),
            ToolCall("probe-b", "probe", {"label": "backend"}),
        ]),
        _text_response("replanned"),
    ])
    agent = Agent(llm=llm, tools=[tool])
    handle = agent.start("inspect both")
    events, thread = _collect(handle)
    assert llm.entered.wait(1)
    handle.steer("only inspect the backend", mode)
    llm.release.set()
    result = handle.result(timeout=3)
    thread.join(1)
    return ScenarioResult(scenario, result=result, events=events,
                          messages=agent.messages, executions=executions)


def append_while_model(workdir: Path) -> ScenarioResult:
    return _steer_during_model(workdir, "append_while_model", SteeringMode.APPEND)


def redirect_while_model(workdir: Path) -> ScenarioResult:
    return _steer_during_model(workdir, "redirect_while_model", SteeringMode.REDIRECT)


def redirect_before_write(workdir: Path) -> ScenarioResult:
    target = workdir / "front.txt"
    llm = GateLLM([
        LLMResponse(tool_calls=[ToolCall("write-a", "write_file", {
            "file_path": str(target), "content": "frontend",
        })]),
        _text_response("backend only"),
    ])
    holder: dict[str, Any] = {}
    agent = Agent(llm=llm, tools=[WriteFileTool()], permission=Permission(allow_all=True))
    handle = agent.start("write both")
    holder["handle"] = handle
    events, thread = _collect(handle)
    assert llm.entered.wait(1)
    handle.steer("skip the frontend write", SteeringMode.REDIRECT)
    llm.release.set()
    result = handle.result(timeout=3)
    thread.join(1)
    return ScenarioResult("redirect_before_write", result=result, events=events,
                          messages=agent.messages, writes=[] if not target.exists() else [str(target)])


def redirect_after_write(workdir: Path) -> ScenarioResult:
    target = workdir / "front.txt"
    llm = GateLLM([
        LLMResponse(tool_calls=[ToolCall("write-a", "write_file", {
            "file_path": str(target), "content": "frontend",
        })]),
        _text_response("backend only"),
    ], gate_calls=(1,))
    holder: dict[str, Any] = {}
    agent = Agent(llm=llm, tools=[WriteFileTool()], permission=Permission(allow_all=True))

    def on_event(event):
        if event.event_type is AgentEventType.TOOL_FINISHED and "handle" in holder:
            holder["handle"].steer("now only inspect backend", SteeringMode.REDIRECT)
        if event.event_type is AgentEventType.MODEL_STARTED and event.public_data.get("round") == 2:
            llm.release.set()

    handle = agent.start("write then inspect", on_event=on_event)
    holder["handle"] = handle
    events, thread = _collect(handle)
    assert llm.entered.wait(1)  # gate call 1, not call 0
    llm.release.set()
    result = handle.result(timeout=4)
    thread.join(1)
    writes = [str(target)] if target.exists() else []
    return ScenarioResult("redirect_after_write", result=result, events=events,
                          messages=agent.messages, writes=writes)


def redirect_between_serial_tools(workdir: Path) -> ScenarioResult:
    first = workdir / "one.txt"
    second = workdir / "two.txt"
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[
            ToolCall("write-a", "slow_write", {"path": str(first), "content": "one"}),
            ToolCall("write-b", "slow_write", {"path": str(second), "content": "two"}),
        ]),
        _text_response("stopped"),
    ])
    tool = SlowWriteProbe()
    agent = Agent(llm=llm, tools=[tool])
    handle = agent.start("write two files")
    events, thread = _collect(handle)
    assert tool.entered.wait(2)
    handle.steer("stop after the first write", SteeringMode.REDIRECT)
    tool.release.set()
    result = handle.result(timeout=4)
    thread.join(1)
    writes = [str(path) for path in (first, second) if path.exists()]
    return ScenarioResult("redirect_between_serial_tools", result=result, events=events,
                          messages=agent.messages, writes=writes)


def redirect_parallel_batch(workdir: Path) -> ScenarioResult:
    executions: list[str] = []
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[
            ToolCall("read-a", "read_probe", {"path": "a"}),
            ToolCall("read-b", "read_probe", {"path": "b"}),
        ]),
        _text_response("noted"),
    ])
    agent = Agent(llm=llm, tools=[ReadProbe(executions, delay=0.2)])
    handle = agent.start("read both")
    events, thread = _collect(handle)

    def steer_after_batch_started():
        _wait_event(events, AgentEventType.TOOL_STARTED)
        handle.steer("replan after this batch", SteeringMode.REDIRECT)

    threading.Thread(target=steer_after_batch_started, daemon=True).start()
    result = handle.result(timeout=4)
    thread.join(1)
    return ScenarioResult("redirect_parallel_batch", result=result, events=events,
                          messages=agent.messages, executions=executions)


def pause_resume(workdir: Path) -> ScenarioResult:
    executions: list[str] = []
    llm = GateLLM([
        LLMResponse(tool_calls=[ToolCall("probe-a", "probe", {"label": "front"})]),
        _text_response("resumed"),
    ])
    agent = Agent(llm=llm, tools=[ExecutionProbe(executions)])
    handle = agent.start("pause check")
    events, thread = _collect(handle)
    assert llm.entered.wait(1)
    handle.pause("eval pause")
    llm.release.set()
    _wait_event(events, AgentEventType.RUN_PAUSED)
    handle.steer("resume with backend only", SteeringMode.APPEND)
    handle.resume()
    result = handle.result(timeout=4)
    thread.join(1)
    return ScenarioResult("pause_resume", result=result, events=events,
                          messages=agent.messages, executions=executions)


def pause_cancel(workdir: Path) -> ScenarioResult:
    executions: list[str] = []
    llm = GateLLM([LLMResponse(tool_calls=[ToolCall("probe-a", "probe", {"label": "x"})])])
    agent = Agent(llm=llm, tools=[ExecutionProbe(executions)])
    handle = agent.start("pause then cancel")
    events, thread = _collect(handle)
    assert llm.entered.wait(1)
    handle.pause("eval pause")
    llm.release.set()
    _wait_event(events, AgentEventType.RUN_PAUSED)
    handle.cancel("eval cancel")
    result = handle.result(timeout=4)
    thread.join(1)
    return ScenarioResult("pause_cancel", result=result, events=events,
                          messages=agent.messages, executions=executions)


def bash_stream(workdir: Path) -> ScenarioResult:
    command = (
        f"{sys.executable} -c \"import time; print('first', flush=True); "
        "time.sleep(0.2); print('second', flush=True)\""
    )
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("bash-1", "bash", {"command": command})]),
        _text_response("done"),
    ])
    agent = Agent(llm=llm, tools=[BashTool()], permission=Permission(allow_all=True))
    handle = agent.start("stream bash")
    events, thread = _collect(handle)
    result = handle.result(timeout=6)
    thread.join(1)
    return ScenarioResult("bash_stream", result=result, events=events,
                          messages=agent.messages)


def bash_cancel(workdir: Path) -> ScenarioResult:
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("bash-1", "bash", {"command": "sleep 10"})]),
    ])
    agent = Agent(llm=llm, tools=[BashTool()], permission=Permission(allow_all=True))
    handle = agent.start("cancel bash")
    events, thread = _collect(handle)

    def cancel_when_started():
        _wait_event(events, AgentEventType.TOOL_STARTED)
        handle.cancel("eval cancels bash")

    threading.Thread(target=cancel_when_started, daemon=True).start()
    result = handle.result(timeout=5)
    thread.join(1)
    return ScenarioResult("bash_cancel", result=result, events=events,
                          messages=agent.messages)


def permission_steering(workdir: Path) -> ScenarioResult:
    executions: list[str] = []
    broker = PermissionRequestBroker()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("confirm-1", "confirm_probe", {"label": "write"})]),
        _text_response("approved and done"),
    ])
    agent = Agent(
        llm=llm,
        tools=[ConfirmProbe(executions)],
        permission=Permission(broker=broker),
    )
    handle = agent.start("confirm a write")
    events, thread = _collect(handle)
    request = broker.poll(timeout=2)
    assert request is not None
    handle.steer("keep it to the backend", SteeringMode.APPEND)
    broker.resolve(request.request_id, "once")
    result = handle.result(timeout=4)
    thread.join(1)
    return ScenarioResult("permission_steering", result=result, events=events,
                          messages=agent.messages, executions=executions)


def crash_resume(workdir: Path) -> ScenarioResult:
    store = RunStore(workdir / "resume.sqlite3")
    state = RunState.create("resume a crashed run", [])
    state.transition(RunStatus.RUNNING)
    store.save_boundary(state, [{"role": "user", "content": "resume a crashed run"}], "running")
    llm = ScriptedLLM([_text_response("recovered without replay")])
    events: list[tuple[float, Any]] = []
    agent = Agent(llm=llm, tools=[], run_store=store)
    result = agent.resume_run(
        state.run_id,
        resume_paused=True,
        on_event=lambda event: events.append((time.monotonic(), event)),
    )
    return ScenarioResult("crash_resume", result=result, events=events,
                          messages=agent.messages)


def sink_exception(workdir: Path) -> ScenarioResult:
    agent = Agent(llm=ScriptedLLM([_text_response("sink survived")]), tools=[])
    handle = agent.start("broken sink", event_sink=ExplodingSink())
    events, thread = _collect(handle)
    result = handle.result(timeout=3)
    thread.join(1)
    return ScenarioResult("sink_exception", result=result, events=events,
                          messages=agent.messages)


def sensitive_input(workdir: Path) -> ScenarioResult:
    store = RunStore(workdir / "sensitive.sqlite3")
    trace_sink = MemoryTraceSink()
    prompt = f"API_KEY={SENSITIVE_MARKER}"
    llm = GateLLM([_text_response("done"), _text_response("done after steering")])
    agent = Agent(llm=llm, tools=[], run_store=store, trace_sink=trace_sink)
    handle = agent.start(prompt)
    events, thread = _collect(handle)
    assert llm.entered.wait(1)
    handle.steer(f"password={SENSITIVE_MARKER}", SteeringMode.APPEND)
    llm.release.set()
    result = handle.result(timeout=4)
    thread.join(1)
    leaks = []
    database = workdir / "sensitive.sqlite3"
    if database.exists() and SENSITIVE_MARKER.encode("utf-8") in database.read_bytes():
        leaks.append("run_store")
    return ScenarioResult(
        "sensitive_input",
        result=result,
        events=events,
        messages=agent.messages,
        trace_events=list(trace_sink.events),
        payload_leaks=leaks,
    )


def backpressure(workdir: Path) -> ScenarioResult:
    command = (
        f"{sys.executable} -c \"[print('line-%04d' % i, flush=True) "
        "for i in range(300)]\""
    )
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("bash-1", "bash", {"command": command})]),
        _text_response("backpressure survived"),
    ])
    agent = Agent(llm=llm, tools=[BashTool()], permission=Permission(allow_all=True))
    handle = agent.start("high frequency", event_queue_size=16)
    events, thread = _collect(handle, slow=0.001)
    result = handle.result(timeout=15)
    thread.join(2)
    return ScenarioResult("backpressure", result=result, events=events,
                          messages=agent.messages)


def repeated_read_cache(workdir: Path) -> ScenarioResult:
    executions: list[str] = []
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[
            ToolCall("read-a", "read_probe", {"path": "same"}),
            ToolCall("read-b", "read_probe", {"path": "same"}),
        ]),
        _text_response("cached"),
    ])
    agent = Agent(llm=llm, tools=[ReadProbe(executions)], tool_cache=True)
    handle = agent.start("read the same thing twice")
    events, thread = _collect(handle)
    result = handle.result(timeout=4)
    thread.join(1)
    return ScenarioResult("repeated_read_cache", result=result, events=events,
                          messages=agent.messages, executions=executions)


def _wait_event(events, event_type, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for _timestamp, event in list(events):
            if event.event_type is event_type:
                return event
        time.sleep(0.01)
    raise AssertionError(f"event not observed: {event_type.value}")


SCENARIOS = {
    "token_streaming": token_streaming,
    "tool_calls_only_progress": tool_calls_only_progress,
    "append_while_model": append_while_model,
    "redirect_while_model": redirect_while_model,
    "redirect_before_write": redirect_before_write,
    "redirect_after_write": redirect_after_write,
    "redirect_between_serial_tools": redirect_between_serial_tools,
    "redirect_parallel_batch": redirect_parallel_batch,
    "pause_resume": pause_resume,
    "pause_cancel": pause_cancel,
    "bash_stream": bash_stream,
    "bash_cancel": bash_cancel,
    "permission_steering": permission_steering,
    "crash_resume": crash_resume,
    "sink_exception": sink_exception,
    "sensitive_input": sensitive_input,
    "backpressure": backpressure,
    "repeated_read_cache": repeated_read_cache,
}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((percentile / 100.0) * (len(ordered) - 1))))
    return round(ordered[index], 3)


def _event_order_violations(events) -> int:
    violations = 0
    previous = 0
    terminal_seen = False
    for _timestamp, event in events:
        if event.sequence <= previous:
            violations += 1
        previous = event.sequence
        if terminal_seen:
            violations += 1
        if event.event_type in {
            AgentEventType.RUN_FINISHED,
            AgentEventType.RUN_FAILED,
            AgentEventType.RUN_CANCELLED,
        }:
            terminal_seen = True
    return violations


def _tool_event_violations(events) -> int:
    violations = 0
    started: set[str] = set()
    terminal: set[str] = set()
    for _timestamp, event in events:
        call_id = event.public_data.get("tool_call_id")
        if event.event_type is AgentEventType.TOOL_STARTED:
            if call_id in started:
                violations += 1
            started.add(call_id)
        elif event.event_type is AgentEventType.TOOL_OUTPUT:
            if call_id not in started or call_id in terminal:
                violations += 1
        elif event.event_type in {AgentEventType.TOOL_FINISHED, AgentEventType.TOOL_FAILED}:
            skipped = event.public_data.get("outcome") in {"blocked", "cancelled"}
            if call_id not in started and not skipped:
                violations += 1
            terminal.add(call_id)
    return violations


def _orphan_messages(messages: list[dict]) -> int:
    expected: list[str] = []
    replies: list[str] = []
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                expected.append(str(call.get("id")))
        elif message.get("role") == "tool":
            replies.append(str(message.get("tool_call_id")))
    return len(set(expected) - set(replies)) + len(set(replies) - set(expected))


def _duplicate_execution_rate(records: list[ScenarioResult]) -> float:
    executed = [item for record in records for item in record.executions]
    if not executed:
        return 0.0
    duplicates = len(executed) - len(set(executed))
    return round(duplicates / len(executed), 4)


def compute_metrics(records: list[ScenarioResult]) -> dict[str, Any]:
    event_records = [record for record in records if record.used_event_stream]
    delivered = [record for record in event_records if record.terminal_seen]
    ack = [
        float(event.public_data.get("ack_ms"))
        for record in records
        for _t, event in record.events
        if event.event_type is AgentEventType.STEERING_RECEIVED
        and event.public_data.get("ack_ms") is not None
    ]
    apply = [
        float(event.public_data.get("apply_ms"))
        for record in records
        for _t, event in record.events
        if event.event_type is AgentEventType.STEERING_APPLIED
        and event.public_data.get("apply_ms") is not None
    ]
    first_progress = []
    for record in event_records:
        if not record.events:
            continue
        start = record.events[0][0]
        for timestamp, event in record.events:
            if event.event_type is not AgentEventType.RUN_STARTED:
                first_progress.append(round((timestamp - start) * 1000.0, 3))
                break
    cancelled = [
        record for record in records
        if record.scenario in {"pause_cancel", "bash_cancel"}
    ]
    cancellation_safe = sum(1 for record in cancelled if not record.writes and not record.executions)
    sensitive_payloads = []
    for record in records:
        for _t, event in record.events:
            sensitive_payloads.append(
                json.dumps(
                    {"message": event.message, "public_data": event.public_data},
                    default=str,
                )
            )
        sensitive_payloads.append(json.dumps(record.trace_events, default=str))
    leak_count = sum(SENSITIVE_MARKER in payload for payload in sensitive_payloads)
    leak_count += sum(len(record.payload_leaks) for record in records)
    orphan_count = sum(_orphan_messages(record.messages) for record in records)
    order_violations = sum(
        _event_order_violations(record.events) + _tool_event_violations(record.events)
        for record in event_records
    )
    deadlocks = sum(1 for record in records if record.timed_out)
    completed = sum(
        1
        for record in records
        if record.result is not None and record.result.status.value != "failed"
    )
    return {
        "scenario_count": len(records),
        "event_delivery_success_rate": round(len(delivered) / max(1, len(event_records)), 4),
        "event_order_violation_count": order_violations,
        "steering_ack_p50_ms": _percentile(ack, 50),
        "steering_ack_p95_ms": _percentile(ack, 95),
        "steering_apply_p50_ms": _percentile(apply, 50),
        "steering_apply_p95_ms": _percentile(apply, 95),
        "cancelled_before_side_effect_rate": round(
            cancellation_safe / max(1, len(cancelled)), 4
        ),
        "orphan_tool_message_count": orphan_count,
        "duplicate_tool_execution_rate": _duplicate_execution_rate(records),
        "time_to_first_progress_p50_ms": _percentile(first_progress, 50),
        "time_to_first_progress_p95_ms": _percentile(first_progress, 95),
        "run_completion_rate": round(completed / max(1, len(records)), 4),
        "deadlock_count": deadlocks,
        "sensitive_payload_leak_count": leak_count,
        "cache_hits": sum(
            1 for record in records for _t, event in record.events
            if event.event_type is AgentEventType.CACHE_HIT
        ),
    }


HARD_GATES = {
    "orphan_tool_message_count": 0,
    "deadlock_count": 0,
    "sensitive_payload_leak_count": 0,
    "event_order_violation_count": 0,
}


def run() -> dict[str, Any]:
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = payload["scenarios"] if isinstance(payload, dict) else payload
    results = []
    records = []
    with tempfile.TemporaryDirectory(prefix="corecoder-steering-eval-") as temp:
        base = Path(temp)
        for index, case in enumerate(cases, 1):
            workdir = base / f"{index:02d}-{case['id']}"
            workdir.mkdir(parents=True, exist_ok=True)
            record = _run_scenario(case["id"], workdir)
            records.append(record)
            results.append({
                "id": case["id"],
                "category": case["category"],
                "status": record.result.status.value if record.result else "error",
                "terminal_seen": record.terminal_seen,
                "event_count": len(record.events),
                "executions": record.executions,
                "writes": record.writes,
                "error": record.error,
                "timed_out": record.timed_out,
            })
    metrics = compute_metrics(records)
    gates = {
        name: {"actual": metrics[name], "expected": expected,
               "passed": metrics[name] == expected}
        for name, expected in HARD_GATES.items()
    }
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "offline_only": True,
        "model": "scripted fake LLM (no API key)",
        "cases": results,
        "metrics": metrics,
        "gates": gates,
        "all_gates_passed": all(item["passed"] for item in gates.values()),
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "steering_eval.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Steerable runtime evaluation",
        "",
        "Offline deterministic run using the real Agent loop and a scripted LLM.",
        "",
        "| metric | value |",
        "| --- | --- |",
    ]
    lines.extend(f"| {name} | {value} |" for name, value in metrics.items())
    lines.extend(["", "## Hard gates", "", "| gate | actual | expected | passed |",
                  "| --- | --- | --- | --- |"])
    for name, gate in gates.items():
        lines.append(
            f"| {name} | {gate['actual']} | {gate['expected']} | {gate['passed']} |"
        )
    (RESULTS_DIR / "steering_eval.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> int:
    report = run()
    print(json.dumps(report["metrics"], indent=2))
    print(json.dumps(report["gates"], indent=2))
    return 0 if report["all_gates_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
