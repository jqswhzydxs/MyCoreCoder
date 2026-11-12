"""Trace the real Agent loop offline, using the existing ScriptedLLM.

Run from the repository root after an editable install:
    python examples/learning_loop_trace.py
    python examples/learning_loop_trace.py --scenario missing
    python examples/learning_loop_trace.py --scenario denied

The model replies are scripted. Tool dispatch, permission checks, file reads,
and message history use the original implementation. No API key is needed.
"""

import argparse
import json
import tempfile
from pathlib import Path

from corecoder import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.tools.read import ReadFileTool
from corecoder.tools.write import WriteFileTool


def show(label, value):
    print(f"\n{label}")
    print(json.dumps(value, ensure_ascii=False, indent=2))


class TracingLLM(ScriptedLLM):
    def __init__(self, script):
        super().__init__(script)
        self.requests = 0

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        self.requests += 1
        # Keep the generated system prompt short on screen. Everything else is
        # shown verbatim, including the tool-call id and the observed result.
        visible = [
            {"role": "system", "content": "[system prompt omitted from display]"}
            if m["role"] == "system" else m
            for m in messages
        ]
        show(f"LLM REQUEST {self.requests}: messages", visible)
        show("AVAILABLE TOOLS", [t["function"]["name"] for t in tools or []])
        response = super().chat(messages, tools, on_token, on_reasoning)
        show(f"LLM RESPONSE {self.requests}", response.message)
        return response


def run(scenario):
    with tempfile.TemporaryDirectory(prefix="corecoder-learning-") as temp_dir:
        fixture = Path(temp_dir) / "note.txt"
        fixture.write_text("hello agent\n", encoding="utf-8")

        if scenario == "denied":
            tool = WriteFileTool()
            arguments = {"file_path": str(fixture), "content": "changed\n"}
            final = "Scripted final reply: the permission layer refused the write."
        else:
            tool = ReadFileTool()
            target = fixture if scenario == "read" else Path(temp_dir) / "missing.txt"
            arguments = {"file_path": str(target)}
            final = "Scripted final reply: " + (
                "the file contains hello agent." if scenario == "read" else "the file was not found."
            )

        llm = TracingLLM([
            LLMResponse(tool_calls=[ToolCall(id="learn-1", name=tool.name, arguments=arguments)]),
            LLMResponse(content=final),
        ])
        agent = Agent(llm=llm, tools=[tool], permission=Permission())
        result = agent.chat(
            f"Learning scenario: {scenario}. Use {tool.name} for {arguments['file_path']}.",
            on_tool=lambda name, args: show("TOOL REQUESTED (before permission / execution)", {name: args}),
        )

        show("FINAL RETURN VALUE", result)
        show("STORED HISTORY (system prompt is added only when sending a request)", agent.messages)

        # Check the observable behavior as well as the protocol pairing. A
        # scripted final reply alone cannot prove a tool actually succeeded.
        assert llm.requests == 2
        assert [m["role"] for m in agent.messages] == ["user", "assistant", "tool", "assistant"]
        observation = agent.messages[2]
        assert observation["tool_call_id"] == "learn-1"
        if scenario == "denied":
            assert "Permission denied" in observation["content"]
        elif scenario == "missing":
            assert "not found" in observation["content"]
        else:
            assert "hello agent" in observation["content"]
        assert fixture.read_text(encoding="utf-8") == "hello agent\n"
        print("\nObserved history and file state match this scenario.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["read", "missing", "denied"], default="read")
    run(parser.parse_args().scenario)
