"""Offline coverage for the steerable interactive CLI runtime."""

import threading
from types import SimpleNamespace
from typing import ClassVar

from corecoder import Agent, Config, PermissionRequestBroker, SteeringMode
from corecoder.agent import STEERING_MESSAGE_PREFIX
from corecoder.cli import (
    CLIEventRenderer,
    _handle_running_input,
    _InteractiveRun,
    _repl,
)
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.tools.base import Tool
from tests.conftest import FakePromptSession


class GatedRecordingLLM:
    model = "interactive-fake"
    total_prompt_tokens = 0
    total_completion_tokens = 0
    estimated_cost = None

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[list[dict]] = []

    def chat(
        self,
        messages,
        tools=None,
        on_token=None,
        on_reasoning=None,
        on_stream_activity=None,
    ) -> LLMResponse:
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("test did not release interactive fake LLM")
        response = self.responses.pop(0)
        if on_token and response.content:
            on_token(response.content)
        if on_stream_activity:
            for index, _call in enumerate(response.tool_calls):
                on_stream_activity({
                    "activity": "tool_call_detected",
                    "tool_index": index,
                })
        return response


class ProbeTool(Tool):
    name = "probe"
    description = "Record one deterministic probe."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    }
    side_effect = "none"
    risk = "low"
    required_permissions: ClassVar[tuple[str, ...]] = ()
    requires_confirmation = False
    idempotent = True
    parallel_safe = False

    def __init__(self, executions: list[str]) -> None:
        self.executions = executions

    def execute(self, label: str) -> str:
        self.executions.append(label)
        return f"probed {label}"


class ConfirmTool(ProbeTool):
    name = "confirm_probe"
    requires_confirmation = True
    side_effect = "write"


def _tool_response(name: str = "probe", label: str = "planned") -> LLMResponse:
    return LLMResponse(tool_calls=[
        ToolCall("call-1", name, {"label": label}),
    ])


def _release_and(llm: GatedRecordingLLM, text: str):
    llm.release.set()
    return text


def _steering_texts(messages: list[dict]) -> list[str]:
    return [
        str(message["content"]).split("\n", 1)[1]
        for message in messages
        if message.get("role") == "user"
        and str(message.get("content", "")).startswith(STEERING_MESSAGE_PREFIX)
    ]


def test_plain_runtime_input_is_redirect_and_cli_remains_responsive(capsys):
    executions: list[str] = []
    llm = GatedRecordingLLM([
        _tool_response(label="obsolete"),
        LLMResponse(content="redirected final"),
    ])
    agent = Agent(llm=llm, tools=[ProbeTool(executions)])
    session = FakePromptSession([
        "initial task",
        "only inspect the backend",
        lambda: _release_and(llm, "/status"),
        "quit",
    ])

    _repl(agent, Config(model=llm.model), prompt_session=session, patch_output=False)

    assert executions == []
    assert _steering_texts(llm.calls[1]) == ["only inspect the backend"]
    assert any(prompt.startswith("Steer") for prompt in session.prompts)
    assert "Pending steering:" in capsys.readouterr().out


def test_append_command_keeps_planned_safe_tool(capsys):
    executions: list[str] = []
    llm = GatedRecordingLLM([
        _tool_response(label="safe"),
        LLMResponse(content="append final"),
    ])
    agent = Agent(llm=llm, tools=[ProbeTool(executions)])
    session = FakePromptSession([
        "initial task",
        "/append keep the safe inspection",
        lambda: _release_and(llm, "/status"),
        "quit",
    ])

    _repl(agent, Config(model=llm.model), prompt_session=session, patch_output=False)

    assert executions == ["safe"]
    assert _steering_texts(llm.calls[1]) == ["keep the safe inspection"]
    assert agent.messages[-1]["content"] == "append final"
    assert "运行完成" in capsys.readouterr().out


def test_pause_resume_cancel_and_explicit_steer_route_to_handle():
    calls = []

    class FakeHandle:
        is_done = False

        def pause(self):
            calls.append(("pause",))

        def resume(self):
            calls.append(("resume",))

        def cancel(self, reason=None):
            calls.append(("cancel", reason))

        def steer(self, text, mode):
            calls.append(("steer", text, mode))

    active = _InteractiveRun(FakeHandle(), SimpleNamespace())
    broker = PermissionRequestBroker()

    _handle_running_input("/pause", active, broker)
    _handle_running_input("/resume", active, broker)
    _handle_running_input("/steer change direction", active, broker)
    _handle_running_input("/append add detail", active, broker)
    _handle_running_input("/cancel", active, broker)

    assert calls == [
        ("pause",),
        ("resume",),
        ("steer", "change direction", SteeringMode.REDIRECT),
        ("steer", "add detail", SteeringMode.APPEND),
        ("cancel", "Cancellation requested from the interactive CLI."),
    ]


def test_permission_broker_uses_only_main_prompt_session(monkeypatch, capsys):
    executions: list[str] = []
    broker = PermissionRequestBroker()
    agent = Agent(
        llm=ScriptedLLM([
            _tool_response(name="confirm_probe", label="approved"),
            LLMResponse(content="permission final"),
        ]),
        tools=[ConfirmTool(executions)],
        permission=Permission(broker=broker),
    )
    session = FakePromptSession(["run confirmed tool", "y", "quit"])
    main_thread = threading.get_ident()
    monkeypatch.setattr(
        "corecoder.cli.pt_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a second terminal input loop was created")
        ),
    )

    _repl(agent, Config(model="scripted"), prompt_session=session, patch_output=False)

    assert executions == ["approved"]
    assert any(prompt.startswith("Permission confirm_probe") for prompt in session.prompts)
    assert set(session.thread_ids) == {main_thread}
    assert "permission: once" in capsys.readouterr().out


def test_permission_broker_timeout_and_close_default_to_deny():
    assert PermissionRequestBroker(timeout=0).request("write", {"content": "secret"}) == "deny"

    broker = PermissionRequestBroker()
    verdicts = []
    worker = threading.Thread(
        target=lambda: verdicts.append(broker.request("bash", {"command": "hidden"})),
    )
    worker.start()
    assert broker.poll(timeout=1) is not None
    broker.close()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert verdicts == ["deny"]


def test_handle_cancel_denies_pending_permission_and_wakes_worker():
    executions: list[str] = []
    broker = PermissionRequestBroker()
    handle = Agent(
        llm=ScriptedLLM([_tool_response(name="confirm_probe", label="blocked")]),
        tools=[ConfirmTool(executions)],
        permission=Permission(broker=broker),
    ).start("cancel permission")

    assert broker.poll(timeout=1) is not None
    handle.cancel("cancel permission wait")
    result = handle.result(timeout=2)

    assert result.status.value == "cancelled"
    assert executions == []
    assert not broker.has_pending


def test_final_text_is_not_duplicated_and_tokens_end_before_lifecycle(capsys):
    phrase = "one unique final answer"
    agent = Agent(llm=ScriptedLLM([LLMResponse(content=phrase)]), tools=[])
    session = FakePromptSession(["answer once", "quit"])

    _repl(agent, Config(model="scripted"), prompt_session=session, patch_output=False)
    output = capsys.readouterr().out

    assert output.count(phrase) == 1
    assert f"{phrase}\n[" in output
    assert "开始分析任务" in output
    assert "运行完成" in output


def test_renderer_preserves_event_sequence_order(capsys):
    handle = Agent(
        llm=ScriptedLLM([LLMResponse(content="ordered tokens")]),
        tools=[],
    ).start("ordered events")
    renderer = CLIEventRenderer(handle)
    renderer.start()
    renderer.join(timeout=2)

    assert renderer.done.is_set()
    assert renderer.sequences == sorted(renderer.sequences)
    assert len(renderer.sequences) == len(set(renderer.sequences))
    assert "ordered tokens" in capsys.readouterr().out


def test_ctrl_c_cancels_active_run_but_exits_when_idle(capsys):
    llm = GatedRecordingLLM([LLMResponse(content="ignored")])
    agent = Agent(llm=llm, tools=[])
    handles = []
    original_start = agent.start

    def capture_start(*args, **kwargs):
        handle = original_start(*args, **kwargs)
        handles.append(handle)
        return handle

    agent.start = capture_start
    active_session = FakePromptSession([
        "cancel me",
        KeyboardInterrupt(),
        lambda: _release_and(llm, "/status"),
        "quit",
    ])

    _repl(
        agent,
        Config(model=llm.model),
        prompt_session=active_session,
        patch_output=False,
    )

    assert handles[0].result(timeout=1).status.value == "cancelled"
    assert "Cancellation requested." in capsys.readouterr().out

    idle_session = FakePromptSession([KeyboardInterrupt()])
    _repl(
        Agent(llm=ScriptedLLM([]), tools=[]),
        Config(model="scripted"),
        prompt_session=idle_session,
        patch_output=False,
    )
    assert "Bye!" in capsys.readouterr().out
