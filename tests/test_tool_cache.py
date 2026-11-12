"""Deterministic coverage for the safe read-only tool cache."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar

from corecoder import Agent, RunBudget
from corecoder.events import AgentEventType
from corecoder.llm import LLMResponse, ToolCall
from corecoder.state import ExitReason
from corecoder.tool_cache import ToolCache
from corecoder.tools.base import Tool
from corecoder.tools.protocol import ToolOutcome, ToolResult, ToolSpec


class ScriptedLLM:
    def __init__(self, responses):
        self.model = "cache-test"
        self.responses = list(responses)
        self.calls = 0

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        self.calls += 1
        return self.responses.pop(0)


def call(call_id: str, name: str, **arguments):
    return ToolCall(id=call_id, name=name, arguments=arguments)


class ReadOnceTool(Tool):
    name = "read_once"
    description = "Read a synthetic path."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    side_effect = "read"
    risk = "low"
    required_permissions = ()
    requires_confirmation = False
    idempotent = True
    parallel_safe = True

    def __init__(self):
        self.calls = 0

    def execute(self, path: str):
        self.calls += 1
        return f"content:{path}"


class VersionedReadTool(Tool):
    name = "versioned_read"
    description = "Read a synthetic versioned file."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}
    side_effect = "read"
    risk = "low"
    required_permissions = ()
    requires_confirmation = False
    idempotent = True
    parallel_safe = True

    def __init__(self):
        self.version = 0
        self.calls = 0

    def execute(self):
        self.calls += 1
        return f"version:{self.version}"


class WriteProbeTool(Tool):
    name = "write_probe"
    description = "Mutate the synthetic file."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}
    side_effect = "write"
    risk = "low"
    required_permissions = ()
    requires_confirmation = False
    idempotent = True
    parallel_safe = False

    def __init__(self, read_tool: VersionedReadTool):
        self.read_tool = read_tool

    def execute(self):
        self.read_tool.version += 1
        return "written"


class ExternalReadTool(Tool):
    name = "external_read"
    description = "Pretend to read a live external service."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}
    side_effect = "external"
    risk = "high"
    required_permissions = ()
    requires_confirmation = False
    idempotent = True
    parallel_safe = False

    def __init__(self):
        self.calls = 0

    def execute(self):
        self.calls += 1
        return "live"


class NoCacheReadTool(Tool):
    name = "no_cache_read"
    description = "A read tool that opts out of caching."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}
    side_effect = "read"
    risk = "low"
    required_permissions = ()
    requires_confirmation = False
    idempotent = True
    parallel_safe = False
    cacheable = False

    def __init__(self):
        self.calls = 0

    def execute(self):
        self.calls += 1
        return "uncached"


def read_spec(**overrides) -> ToolSpec:
    values = {
        "name": "read_once",
        "description": "read",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "string"},
        "side_effect": "read",
        "idempotent": True,
    }
    values.update(overrides)
    return ToolSpec(**values)


def test_identical_read_only_call_executes_once_and_reuses_model_content():
    tool = ReadOnceTool()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[call("one", tool.name, path="a.py")]),
        LLMResponse(tool_calls=[call("two", tool.name, path="a.py")]),
        LLMResponse(content="done"),
    ])
    agent = Agent(llm=llm, tools=[tool])

    result = agent.run("read a.py")

    assert tool.calls == 1
    assert result.metrics["tool_calls"] == 2
    assert result.metrics["cache_hits"] == 1
    tool_messages = [message for message in agent.messages if message["role"] == "tool"]
    assert [message["content"] for message in tool_messages] == [
        "content:a.py",
        "content:a.py",
    ]
    assert agent.tool_cache.stats["hits"] == 1


def test_cache_hit_event_is_ordered_and_does_not_leak_arguments():
    tool = ReadOnceTool()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[call("one", tool.name, path="secret.py")]),
        LLMResponse(tool_calls=[call("two", tool.name, path="secret.py")]),
        LLMResponse(content="done"),
    ])
    events = []
    agent = Agent(llm=llm, tools=[tool])
    agent.run("read", on_event=events.append)

    relevant = [
        event for event in events
        if event.public_data.get("tool_call_id") == "two"
        and event.event_type in {
            AgentEventType.TOOL_STARTED,
            AgentEventType.CACHE_HIT,
            AgentEventType.TOOL_FINISHED,
        }
    ]
    assert [event.event_type for event in relevant] == [
        AgentEventType.TOOL_STARTED,
        AgentEventType.CACHE_HIT,
        AgentEventType.TOOL_FINISHED,
    ]
    cache_events = [event for event in events if event.event_type is AgentEventType.CACHE_HIT]
    assert len(cache_events) == 1
    assert "secret.py" not in repr(cache_events[0].public_data)


def test_cache_can_be_disabled_per_agent():
    tool = ReadOnceTool()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[call("one", tool.name, path="a.py")]),
        LLMResponse(tool_calls=[call("two", tool.name, path="a.py")]),
        LLMResponse(content="done"),
    ])
    agent = Agent(llm=llm, tools=[tool], tool_cache=False)

    result = agent.run("read")

    assert tool.calls == 2
    assert result.metrics["cache_hits"] == 0


def test_write_invalidates_cached_read_before_it_executes():
    read_tool = VersionedReadTool()
    write_tool = WriteProbeTool(read_tool)
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[call("one", read_tool.name)]),
        LLMResponse(tool_calls=[call("write", write_tool.name)]),
        LLMResponse(tool_calls=[call("two", read_tool.name)]),
        LLMResponse(content="done"),
    ])
    agent = Agent(llm=llm, tools=[read_tool, write_tool])

    result = agent.run("read, write, read")

    assert read_tool.calls == 2
    tool_messages = [message["content"] for message in agent.messages if message["role"] == "tool"]
    assert tool_messages == ["version:0", "written", "version:1"]
    assert result.metrics["cache_hits"] == 0


def test_external_and_explicitly_uncacheable_tools_are_never_cached():
    external = ExternalReadTool()
    no_cache = NoCacheReadTool()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[
            call("one", external.name),
            call("two", no_cache.name),
        ]),
        LLMResponse(tool_calls=[
            call("three", external.name),
            call("four", no_cache.name),
        ]),
        LLMResponse(content="done"),
    ])
    agent = Agent(llm=llm, tools=[external, no_cache])

    agent.run("two uncacheable tools")

    assert external.calls == 2
    assert no_cache.calls == 2
    assert agent.tool_cache.stats["hits"] == 0


def test_cache_key_includes_cwd_version_arguments_and_revision(tmp_path):
    cache = ToolCache()
    spec = read_spec()
    other_version = read_spec(version="2")
    arguments = {"path": "a.py"}

    first = cache.make_key(spec, arguments, cwd=str(tmp_path))
    assert first != cache.make_key(spec, arguments, cwd=str(tmp_path / "nested"))
    assert first != cache.make_key(other_version, arguments, cwd=str(tmp_path))
    assert first != cache.make_key(spec, {"path": "b.py"}, cwd=str(tmp_path))
    cache.invalidate("manual")
    assert first != cache.make_key(spec, arguments, cwd=str(tmp_path))


def test_cache_single_flight_and_call_id_rebinding(tmp_path):
    cache = ToolCache()
    spec = read_spec()
    calls = 0

    def execute():
        nonlocal calls
        calls += 1
        return ToolResult.success("original", spec.name, "payload", evidence=("tool:original:read_once",))

    hit1, result1, first_key = cache.get_or_execute(
        spec,
        {"path": "a.py"},
        call_id="original",
        cwd=str(tmp_path),
        execute=execute,
    )
    hit2, result2, second_key = cache.get_or_execute(
        spec,
        {"path": "a.py"},
        call_id="second",
        cwd=str(tmp_path),
        execute=execute,
    )

    assert (hit1, hit2) == (False, True)
    assert first_key == second_key
    assert calls == 1
    assert result1.model_content() == result2.model_content() == "payload"
    assert result2.call_id == "second"
    assert result2.evidence == ("tool:second:read_once",)


def test_cache_does_not_store_failures(tmp_path):
    cache = ToolCache()
    spec = read_spec()
    calls = 0

    def execute():
        nonlocal calls
        calls += 1
        return ToolResult.failure(
            "bad",
            spec.name,
            "failed",
            outcome=ToolOutcome.UNKNOWN_ERROR,
            error_category="test",
            error_code="test_error",
        )

    for call_id in ("one", "two"):
        cache.get_or_execute(
            spec,
            {"path": "a.py"},
            call_id=call_id,
            cwd=str(tmp_path),
            execute=execute,
        )

    assert calls == 2
    assert cache.stats["entries"] == 0


def test_cache_eligibility_requires_read_idempotent_and_enabled():
    cache = ToolCache()
    assert cache.is_cacheable(read_spec())
    assert not cache.is_cacheable(read_spec(side_effect="write"))
    assert not cache.is_cacheable(read_spec(side_effect="external"))
    assert not cache.is_cacheable(read_spec(idempotent=False))
    assert not cache.is_cacheable(read_spec(cacheable=False))
    cache.enabled = False
    assert not cache.is_cacheable(read_spec())


def test_cache_single_flight_under_concurrent_callers(tmp_path):
    cache = ToolCache()
    spec = read_spec()
    calls = 0
    calls_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def execute():
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return ToolResult.success("original", spec.name, "payload")

    def worker(index: int):
        barrier.wait(timeout=5)
        return cache.get_or_execute(
            spec,
            {"path": "a.py"},
            call_id=f"call-{index}",
            cwd=str(tmp_path),
            execute=execute,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))

    assert calls == 1
    assert sum(1 for hit, _result, _key in results if hit) == 7
    assert {result.model_content() for _hit, result, _key in results} == {"payload"}
    for index, (hit, result, _key) in enumerate(results):
        if hit:
            assert result.call_id == f"call-{index}"


def test_manual_invalidation_forces_the_next_execution(tmp_path):
    cache = ToolCache()
    spec = read_spec()
    calls = 0

    def execute():
        nonlocal calls
        calls += 1
        return ToolResult.success("original", spec.name, f"value-{calls}")

    calls_and_hits = []
    for call_id in ("one", "two"):
        calls_and_hits.append(
            cache.get_or_execute(
                spec,
                {"path": "a.py"},
                call_id=call_id,
                cwd=str(tmp_path),
                execute=execute,
            )[:2]
        )
    assert calls_and_hits[0][0] is False
    assert calls_and_hits[1][0] is True
    assert calls == 1

    cache.invalidate("user_requested_refresh")
    _hit, refreshed, _key = cache.get_or_execute(
        spec,
        {"path": "a.py"},
        call_id="three",
        cwd=str(tmp_path),
        execute=execute,
    )

    assert calls == 2
    assert refreshed.model_content() == "value-2"


def test_bash_is_never_cacheable():
    from corecoder.tools.bash import BashTool

    assert not ToolCache().is_cacheable(BashTool().spec)


def test_cached_repeat_still_counts_toward_no_progress_budget():
    tool = ReadOnceTool()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[call("one", tool.name, path="a.py")]),
        LLMResponse(tool_calls=[call("two", tool.name, path="a.py")]),
        LLMResponse(tool_calls=[call("three", tool.name, path="a.py")]),
        LLMResponse(content="must not run"),
    ])
    agent = Agent(llm=llm, tools=[tool])

    result = agent.run(
        "repeat",
        budget=RunBudget(max_rounds=10, max_no_progress_steps=2),
    )

    assert result.exit_reason is ExitReason.NO_PROGRESS
    assert tool.calls == 1
    assert result.budget_usage["tool_calls"] == 3
    assert result.budget_usage["no_progress_steps"] == 2
    assert llm.calls == 3


def test_cache_saves_real_executions_without_changing_rounds_or_answers():
    def run(cache_enabled: bool):
        tool = ReadOnceTool()
        llm = ScriptedLLM([
            LLMResponse(tool_calls=[call("one", tool.name, path="a.py")]),
            LLMResponse(tool_calls=[call("two", tool.name, path="a.py")]),
            LLMResponse(tool_calls=[call("three", tool.name, path="a.py")]),
            LLMResponse(content="same answer"),
        ])
        agent = Agent(llm=llm, tools=[tool], tool_cache=cache_enabled)
        result = agent.run(
            "repeat",
            budget=RunBudget(max_rounds=10, max_no_progress_steps=10),
        )
        return tool, llm, result

    plain_tool, plain_llm, plain = run(False)
    cached_tool, cached_llm, cached = run(True)

    # Real side effects: three executes before, one execute and two hits after.
    assert plain_tool.calls == 3
    assert cached_tool.calls == 1
    assert plain.metrics["cache_hits"] == 0
    assert cached.metrics["cache_hits"] == 2
    assert plain.metrics["tool_executions"] == 3
    assert cached.metrics["tool_executions"] == 1

    # The optimization must not change the protocol-visible work or answer.
    assert plain.metrics["tool_calls"] == cached.metrics["tool_calls"] == 3
    assert plain.metrics["model_calls"] == cached.metrics["model_calls"]
    assert plain_llm.calls == cached_llm.calls == 4
    assert plain.final_text == cached.final_text == "same answer"
    assert plain.status is cached.status
