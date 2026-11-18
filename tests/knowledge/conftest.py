import importlib.util
import os
import socket

import pytest

if os.getenv("CORECODER_REQUIRE_KNOWLEDGE_TESTS") == "1":
    missing = [name for name in ("pathspec", "langchain_text_splitters", "langchain_chroma", "chromadb")
               if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError("knowledge CI requires all optional dependencies: " + ", ".join(missing))


@pytest.fixture(autouse=True)
def offline_knowledge(monkeypatch):
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "False")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")

    def blocked(*args, **kwargs):
        raise AssertionError("knowledge tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
