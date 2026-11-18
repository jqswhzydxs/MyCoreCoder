import importlib.util
import os
import socket

import pytest

os.environ.setdefault("LANGSMITH_TRACING", "false")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
if os.getenv("CORECODER_REQUIRE_GRAPH_TESTS") == "1" and (
        importlib.util.find_spec("langgraph") is None or importlib.util.find_spec("langgraph.checkpoint.sqlite") is None):
    raise RuntimeError("graph CI requires langgraph and its SQLite checkpointer")


@pytest.fixture(autouse=True)
def offline_workflow(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("workflow tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
