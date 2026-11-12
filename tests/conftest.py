"""Shared pytest fixtures and helpers."""

import threading
import time

import pytest

from corecoder import Config, checkpoints, cli
from corecoder import llm as llm_module
from corecoder.tools import ALL_TOOLS


@pytest.fixture(autouse=True)
def _checkpoints_isolated(tmp_path, monkeypatch):
    """Checkpoint persistence must never touch the real ~/.corecoder."""
    monkeypatch.setattr(checkpoints, "CHECKPOINTS_FILE", tmp_path / "checkpoints.json")
    checkpoints.clear()
    yield
    checkpoints.clear()


@pytest.fixture(autouse=True)
def _pricing_isolated(tmp_path, monkeypatch):
    """Cost estimates must never read the real ~/.corecoder/pricing.json."""
    monkeypatch.setattr(llm_module, "PRICING_FILE", tmp_path / "pricing.json")


def get_tool(name: str):
    """Look up a tool by name."""
    for t in ALL_TOOLS:
        if t.name == name:
            return t
    return None


class FakePromptSession:
    """Scripted single-reader PromptSession used by offline CLI tests."""

    def __init__(self, inputs):
        self.inputs = list(inputs)
        self.prompts: list[str] = []
        self.thread_ids: list[int] = []
        self.app = self

    def prompt(self, message, *args, **kwargs):
        label = message() if callable(message) else str(message)
        next_text = str(self.inputs[0]).lower() if self.inputs else ""
        if self.inputs and next_text in {
            "quit", "exit", "/quit", "/exit", "y", "yes", "a", "always", "n", "no",
        }:
            deadline = time.monotonic() + 2
            while (
                label.startswith("Steer")
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
                label = message() if callable(message) else str(message)
                if next_text not in {"quit", "exit", "/quit", "/exit"} and label.startswith("Permission"):
                    break
        self.prompts.append(label)
        self.thread_ids.append(threading.get_ident())
        if not self.inputs:
            raise EOFError
        value = self.inputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            value = value()
        return str(value)

    def invalidate(self):
        return None


def repl_with(monkeypatch, agent, inputs, config=None):
    """Drive the real REPL with a scripted list of inputs. Returns the config in use."""
    session = FakePromptSession(inputs)
    config = config or Config.from_env()
    cli._repl(agent, config, prompt_session=session, patch_output=False)
    return config
