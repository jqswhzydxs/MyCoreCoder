"""Offline demo: watch a run and redirect it while the model is thinking.

Run with:

    python examples/steerable_agent_demo.py

No API key is used.  A scripted model, the real Agent loop, the real scheduler
and the real RunStore produce every event and latency printed below.
"""

from __future__ import annotations

import itertools
import tempfile
import threading
import time
from pathlib import Path

from corecoder import Agent, AgentEventType, SteeringMode
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.tools import ReadFileTool


class GateLLM(ScriptedLLM):
    """Hold the first model call so the user can steer mid-flight."""

    def __init__(self, script):
        super().__init__(script, model="offline-scripted-demo")
        self.entered = threading.Event()
        self.release = threading.Event()
        self._calls = 0

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None,
             on_stream_activity=None):
        index = self._calls
        self._calls += 1
        if index == 0:
            self.entered.set()
            self.release.wait(5)
        return super().chat(
            messages,
            tools=tools,
            on_token=on_token,
            on_reasoning=on_reasoning,
            on_stream_activity=on_stream_activity,
        )


def make_workspace(base: Path) -> tuple[Path, Path]:
    frontend = base / "frontend" / "app.py"
    backend = base / "backend" / "api.py"
    frontend.parent.mkdir(parents=True)
    backend.parent.mkdir(parents=True)
    frontend.write_text("def render_frontend():\n    return 'frontend'\n", encoding="utf-8")
    backend.write_text("def backend_health():\n    return {'ok': True}\n", encoding="utf-8")
    return frontend, backend


EVENT_LABELS = {
    AgentEventType.RUN_STARTED: "run started",
    AgentEventType.MODEL_STARTED: "model thinking",
    AgentEventType.MODEL_STREAM_ACTIVITY: "model activity",
    AgentEventType.MODEL_FINISHED: "model response ready",
    AgentEventType.TOOL_PLANNED: "tool planned",
    AgentEventType.TOOL_STARTED: "tool started",
    AgentEventType.TOOL_OUTPUT: "tool output",
    AgentEventType.TOOL_FINISHED: "tool finished",
    AgentEventType.TOOL_FAILED: "tool stopped",
    AgentEventType.STEERING_RECEIVED: "steering received, waiting for safe point",
    AgentEventType.STEERING_APPLIED: "steering applied at safe point",
    AgentEventType.REPLANNING: "replanning",
    AgentEventType.VERIFICATION_STARTED: "verification started",
    AgentEventType.VERIFICATION_FINISHED: "verification finished",
    AgentEventType.RUN_FINISHED: "run finished",
    AgentEventType.RUN_FAILED: "run failed",
    AgentEventType.RUN_CANCELLED: "run cancelled",
}


def main() -> int:
    started = time.monotonic()

    def stamp() -> str:
        return f"[{time.monotonic() - started:05.2f}]"

    with tempfile.TemporaryDirectory(prefix="corecoder-steer-demo-") as temp:
        frontend, backend = make_workspace(Path(temp))
        llm = GateLLM([
            LLMResponse(tool_calls=[
                ToolCall("read-front", "read_file", {"file_path": str(frontend)}),
                ToolCall("read-back", "read_file", {"file_path": str(backend)}),
            ]),
            LLMResponse(tool_calls=[
                ToolCall("read-back-only", "read_file", {"file_path": str(backend)}),
            ]),
            LLMResponse(content="Backend analysis complete: backend_health() is the public API."),
        ])
        agent = Agent(llm=llm, tools=[ReadFileTool()])
        handle = agent.start(
            {
                "goal": "Analyze the frontend and backend, then summarize the API.",
                "verifier": lambda _snapshot: True,
            }
        )

        events = []

        def render() -> None:
            for event in handle.events():
                events.append(event)
                if event.event_type is AgentEventType.MODEL_STREAM_ACTIVITY:
                    activity = event.public_data.get("activity")
                    if activity != "tool_call_detected":
                        continue
                label = EVENT_LABELS.get(event.event_type, event.event_type.value)
                detail = ""
                if event.event_type in {
                    AgentEventType.TOOL_STARTED,
                    AgentEventType.TOOL_FINISHED,
                    AgentEventType.TOOL_FAILED,
                }:
                    detail = f" {event.public_data.get('tool_name', '')}"
                print(f"{stamp()} {label}{detail}", flush=True)

        renderer = threading.Thread(target=render, daemon=True)
        renderer.start()

        if not llm.entered.wait(3):
            print("model gate was not reached", flush=True)
            return 1
        print(f"{stamp()} Steer > analyze the backend only; skip the frontend", flush=True)
        handle.steer("analyze the backend only; skip the frontend", SteeringMode.REDIRECT)
        llm.release.set()

        result = handle.result(timeout=10)
        renderer.join(timeout=2)

        ack = [
            event.public_data["ack_ms"] for event in events
            if event.event_type is AgentEventType.STEERING_RECEIVED
        ]
        applied = [
            event.public_data["apply_ms"] for event in events
            if event.event_type is AgentEventType.STEERING_APPLIED
        ]
        skipped = [
            event.public_data.get("skipped_tool_count") for event in events
            if event.event_type is AgentEventType.STEERING_APPLIED
        ]
        sequences = [event.sequence for event in events]
        ordered = all(left < right for left, right in itertools.pairwise(sequences))
        terminal_last = bool(events) and events[-1].event_type in {
            AgentEventType.RUN_FINISHED,
            AgentEventType.RUN_FAILED,
            AgentEventType.RUN_CANCELLED,
        }
        tool_starts = sum(
            1 for event in events if event.event_type is AgentEventType.TOOL_STARTED
        )
        metrics = result.metrics
        print()
        print("== Run summary ==")
        print(f"status             : {result.status.value}")
        print(f"verification       : {result.verification_status.value}")
        print(f"final text         : {result.final_text}")
        print(f"steering ack       : {ack[0] if ack else 'n/a'} ms")
        print(f"steering apply     : {applied[0] if applied else 'n/a'} ms")
        print(f"skipped tool calls : {skipped[0] if skipped else 0}")
        print(f"tools started      : {tool_starts}")
        print(f"events             : {len(events)}")
        print(f"sequence monotonic : {ordered}")
        print(f"terminal last      : {terminal_last}")
        print(f"cache hits         : {metrics.get('cache_hits', 0)}")
        print(f"total              : {metrics.get('total_duration_ms')} ms")
        print()
        print("Expected: the frontend/backend plan is stopped before either read; "
              "the replanned batch reads only the backend and verification passes.")
        return 0 if result.status.value == "verified" and ordered and terminal_last else 1


if __name__ == "__main__":
    raise SystemExit(main())
