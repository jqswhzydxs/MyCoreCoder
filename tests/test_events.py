"""Deterministic offline coverage for typed Agent UI events."""

import json
from types import SimpleNamespace
from typing import ClassVar

from corecoder import Agent
from corecoder.demo import ScriptedLLM
from corecoder.events import (
    AgentEventType,
    CallbackEventSink,
    CompositeEventSink,
    EventEmitter,
    QueueEventSink,
)
from corecoder.llm import LLM, LLMResponse, ToolCall
from corecoder.tools.base import Tool
from corecoder.trace import MemoryTraceSink


class ProbeTool(Tool):
    name = "probe"
    description = "Return a deterministic result without side effects."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["command", "content"],
    }
    side_effect = "none"
    risk = "low"
    required_permissions: ClassVar[tuple[str, ...]] = ()
    requires_confirmation = False
    idempotent = True
    parallel_safe = False

    def execute(self, command: str, content: str) -> str:
        return f"observed {len(command) + len(content)} bytes"


def _scripted_tool_run():
    return ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall(
            id="probe-1",
            name="probe",
            arguments={
                "command": "echo do-not-publish",
                "content": "private file content",
            },
        )]),
        LLMResponse(content="done"),
    ])


def _event_types(events):
    return [event.event_type for event in events]


def test_run_events_have_strictly_increasing_sequence_and_finish_last():
    sink = QueueEventSink()
    result = Agent(llm=_scripted_tool_run(), tools=[ProbeTool()]).run(
        "observe the run",
        event_sink=sink,
    )

    events = sink.drain()
    sequences = [event.sequence for event in events]

    assert result.final_text == "done"
    assert sequences == list(range(1, len(events) + 1))
    assert events[-1].event_type is AgentEventType.RUN_FINISHED
    assert AgentEventType.MODEL_STARTED in _event_types(events)
    assert AgentEventType.TOOL_PLANNED in _event_types(events)
    assert AgentEventType.VERIFICATION_STARTED in _event_types(events)
    assert AgentEventType.VERIFICATION_FINISHED in _event_types(events)


def test_failing_sink_never_fails_agent_or_other_composite_sink():
    class FailingSink:
        def emit(self, event):
            raise RuntimeError("observer is broken")

    retained = QueueEventSink()
    sink = CompositeEventSink(FailingSink(), retained)

    result = Agent(
        llm=ScriptedLLM([LLMResponse(content="still succeeds")]),
        tools=[],
    ).run("hello", event_sink=sink)

    assert result.final_text == "still succeeds"
    assert retained.drain()[-1].event_type is AgentEventType.RUN_FINISHED


def test_public_data_is_recursively_redacted():
    sink = QueueEventSink()
    emitter = EventEmitter("run-redaction", sink)
    emitter.emit(
        AgentEventType.MODEL_STARTED,
        phase="model",
        public_data={
            "api_key": "sk-super-secret-value",
            "nested": {
                "Authorization": "Bearer abcdefghijklmnop",
                "password": "hunter2",
                "system_prompt": "complete private system instructions",
                "note": "api_key=also-secret",
            },
        },
    )

    event = sink.get_nowait()
    encoded = json.dumps(event.to_dict(), ensure_ascii=False)

    assert event.public_data["api_key"] == "[REDACTED]"
    assert event.public_data["nested"]["Authorization"] == "[REDACTED]"
    assert event.public_data["nested"]["password"] == "[REDACTED]"
    assert event.public_data["nested"]["system_prompt"] == "[REDACTED]"
    assert "super-secret" not in encoded
    assert "hunter2" not in encoded
    assert "complete private system instructions" not in encoded
    assert "also-secret" not in encoded


def test_legacy_callbacks_still_run_with_events_enabled():
    class LegacyLLM:
        model = "legacy-callbacks"
        total_prompt_tokens = 0
        total_completion_tokens = 0
        estimated_cost = None

        def __init__(self):
            self.responses = [
                LLMResponse(
                    content="working",
                    tool_calls=[ToolCall(
                        "probe-1",
                        "probe",
                        {"command": "echo hi", "content": "x"},
                    )],
                ),
                LLMResponse(content="done"),
            ]

        # Deliberately keep the old signature: Agent must not require the new
        # LLM stream-activity keyword from third-party implementations.
        def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
            response = self.responses.pop(0)
            if on_reasoning:
                on_reasoning("private reasoning")
            if on_token and response.content:
                on_token(response.content)
            return response

    tokens = []
    tools = []
    reasoning = []
    events = []
    result = Agent(llm=LegacyLLM(), tools=[ProbeTool()]).run(
        "use callbacks",
        on_token=tokens.append,
        on_tool=lambda name, arguments: tools.append((name, arguments)),
        on_reasoning=reasoning.append,
        on_event=events.append,
    )

    assert result.final_text == "done"
    assert tokens == ["working", "done"]
    assert [name for name, _arguments in tools] == ["probe"]
    assert reasoning == ["private reasoning", "private reasoning"]
    assert AgentEventType.ASSISTANT_TOKEN in _event_types(events)
    assert "private reasoning" not in json.dumps(
        [event.to_dict() for event in events],
        ensure_ascii=False,
    )


def test_plain_text_stream_produces_assistant_token_not_trace_event():
    event_sink = QueueEventSink()
    trace_sink = MemoryTraceSink()
    agent = Agent(
        llm=ScriptedLLM([LLMResponse(content="visible answer")]),
        tools=[],
        trace_sink=trace_sink,
    )

    agent.run("answer", event_sink=event_sink)

    events = event_sink.drain()
    tokens = [
        event.message
        for event in events
        if event.event_type is AgentEventType.ASSISTANT_TOKEN
    ]
    assert tokens == ["visible answer"]
    assert "assistant_token" not in [event["event_type"] for event in trace_sink.events]


class LocalStreamingLLM(LLM):
    """Use the real LLM stream drain with local chunks and no network/client."""

    def __init__(self, streams):
        self.model = "local-stream"
        self.extra = {}
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._streams = list(streams)

    def _call_with_retry(self, params: dict, max_retries: int = 3):
        return iter(self._streams.pop(0))


def _chunk(*, content=None, tool_calls=None, reasoning=None):
    delta = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning,
    )
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta)])


def _tool_delta(index, *, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_tool_call_only_stream_reports_activity_before_model_finishes():
    first_stream = [
        _chunk(tool_calls=[_tool_delta(
            0,
            call_id="probe-1",
            name="probe",
            arguments='{"command":"echo hidden",',
        )]),
        _chunk(tool_calls=[_tool_delta(
            0,
            arguments='"content":"private"}',
        )]),
    ]
    second_stream = [_chunk(content="done")]
    sink = QueueEventSink()

    Agent(
        llm=LocalStreamingLLM([first_stream, second_stream]),
        tools=[ProbeTool()],
    ).run("use a tool", event_sink=sink)

    events = sink.drain()
    activity_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type is AgentEventType.MODEL_STREAM_ACTIVITY
        and event.public_data["activity"] == "tool_call_detected"
    )
    first_model_finished = next(
        index
        for index, event in enumerate(events)
        if event.event_type is AgentEventType.MODEL_FINISHED
    )
    activity = events[activity_index]

    assert activity_index < first_model_finished
    assert activity.public_data["tool_index"] == 0
    encoded = json.dumps(activity.to_dict(), ensure_ascii=False)
    assert "echo hidden" not in encoded
    assert '"content":"private"' not in encoded


def test_tool_started_precedes_terminal_event_and_hides_argument_values():
    sink = QueueEventSink()
    Agent(llm=_scripted_tool_run(), tools=[ProbeTool()]).run(
        "run the probe",
        event_sink=sink,
    )
    events = sink.drain()

    started = next(
        event for event in events
        if event.event_type is AgentEventType.TOOL_STARTED
    )
    terminal = next(
        event for event in events
        if event.event_type in {
            AgentEventType.TOOL_FINISHED,
            AgentEventType.TOOL_FAILED,
        }
    )
    encoded = json.dumps(started.to_dict(), ensure_ascii=False)

    assert started.sequence < terminal.sequence
    assert started.public_data["tool_name"] == "probe"
    assert started.public_data["argument_names"] == ["command", "content"]
    assert "echo do-not-publish" not in encoded
    assert "private file content" not in encoded


def test_callback_and_queue_sinks_both_receive_same_events():
    callback_events = []
    queue_sink = QueueEventSink()
    emitter = EventEmitter(
        "run-composite",
        CompositeEventSink(
            CallbackEventSink(callback_events.append),
            queue_sink,
        ),
    )

    emitted = emitter.emit(
        AgentEventType.PHASE_CHANGED,
        phase="model",
        message="phase",
    )

    assert callback_events == [emitted]
    assert queue_sink.get_nowait() == emitted
