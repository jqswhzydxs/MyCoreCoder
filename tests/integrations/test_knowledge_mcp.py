"""Actual official SDK transports, independent client and original Agent loop."""

import asyncio
import ctypes
import importlib.util
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

if os.getenv("CORECODER_REQUIRE_MCP_TESTS") == "1":
    for dependency in ("mcp", "chromadb", "langchain_core", "langchain_text_splitters", "pathspec"):
        if importlib.util.find_spec(dependency) is None:
            raise RuntimeError("MCP CI requires dependency: " + dependency)
pytest.importorskip("mcp")
pytest.importorskip("chromadb")

from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError as SDKProtocolError

from corecoder import Agent
from corecoder.demo import ScriptedLLM
from corecoder.integrations.mcp_sdk_bridge import MCPToolBridge, SDKBridgeError, SDKServerConfig
from corecoder.knowledge.embedding_gateway import EmbeddingGateway
from corecoder.knowledge.embeddings import FixtureEmbeddingProvider
from corecoder.knowledge.scope import create_scope
from corecoder.knowledge.service import KnowledgeService
from corecoder.llm import LLMResponse, ToolCall
from corecoder.mcp import MCPClient, load_mcp_tools
from corecoder.permissions import Permission
from corecoder.tools.protocol import ToolError, ToolExecutionContext
from corecoder.workflows.budget_ledger import BudgetLedger

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def index(root):
    (root / "corecoder").mkdir(exist_ok=True)
    (root / "corecoder/journal.py").write_text('class RunStore:\n    """Journal execution facts."""\n', encoding="utf-8")
    with KnowledgeService(create_scope(root), EmbeddingGateway(FixtureEmbeddingProvider(64), BudgetLedger())) as service:
        return service.index().snapshot_id


def server_config(root, pid, *, mode="lexical", **kwargs):
    return SDKServerConfig("knowledge", sys.executable,
        (str(FIXTURES / "offline_knowledge_mcp.py"), str(pid), "--root", str(root), "--mode", mode, "--embedding-provider", "fixture"), **kwargs)


def process_alive(pid):
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("protocol_mode", ["auto", "legacy"])
def test_independent_sdk_client_discovers_searches_reads_and_rejects_boundaries(tmp_path, protocol_mode):
    snapshot = index(tmp_path)
    pid_path = tmp_path / "server.pid"
    config = server_config(tmp_path, pid_path)

    async def invoke():
        params = StdioServerParameters(command=config.command, args=list(config.args))
        async with Client(params, mode=protocol_mode, read_timeout_seconds=20, cache=None) as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == {"knowledge_search", "knowledge_get_chunk"}
            response = await client.call_tool("knowledge_search", {"query": "RunStore"})
            assert not response.is_error
            value = response.structured_content
            assert value["snapshot_id"] == snapshot and value["server_budget"]["requests"] == 0
            chunk_id = value["hits"][0]["chunk_id"]
            chunk = await client.call_tool("knowledge_get_chunk", {"chunk_id": chunk_id, "snapshot_id": snapshot})
            assert not chunk.is_error and "RunStore" in chunk.structured_content["chunk"]["text"]
            assert (await client.call_tool("knowledge_search", {"query": "RunStore", "top_k": 100})).is_error
            with pytest.raises(SDKProtocolError):
                await client.call_tool("knowledge_search", {"query": "RunStore", "root": "../foreign"})
            assert (await client.call_tool("knowledge_get_chunk", {"chunk_id": "0" * 64, "snapshot_id": snapshot})).is_error
            (tmp_path / "corecoder/journal.py").write_text("# changed\n", encoding="utf-8")
            assert (await client.call_tool("knowledge_get_chunk", {"chunk_id": chunk_id, "snapshot_id": snapshot})).is_error
            return client.protocol_version

    protocol = asyncio.run(invoke())
    assert protocol == "2026-07-28" if protocol_mode == "auto" else protocol.startswith("2025-")
    assert not process_alive(int(pid_path.read_text(encoding="ascii")))


def test_bridge_feeds_real_agent_and_can_be_called_under_an_existing_loop(tmp_path):
    snapshot = index(tmp_path)
    pid_path = tmp_path / "server.pid"
    with MCPToolBridge(server_config(tmp_path, pid_path)) as bridge:
        tool = next(item for item in bridge.tools if item.remote_name == "knowledge_search")
        assert tool.side_effect == "external" and tool.requires_confirmation and not tool.idempotent and not tool.cacheable
        agent = Agent(ScriptedLLM([LLMResponse(tool_calls=[ToolCall("search", tool.name, {"query": "RunStore"})]),
                                   LLMResponse(content="answer")]), tools=[tool], permission=Permission(allow_all=True))
        result = agent.run("Explain RunStore")
        assert result.status.value == "unverified" and agent.tool_results[0].ok
        assert agent.tool_results[0].data["snapshot_id"] == snapshot

        async def nested_caller_loop():
            assert tool.execute(query="RunStore").ok

        asyncio.run(nested_caller_loop())
        first_pid = int(pid_path.read_text(encoding="ascii"))
        bridge.reconnect()
        assert not process_alive(first_pid) and tool.execute(query="RunStore").ok
        last_pid = int(pid_path.read_text(encoding="ascii"))
    assert bridge.closed and not process_alive(last_pid)
    with pytest.raises(SDKBridgeError, match="connection_closed"):
        bridge.call_tool("knowledge_search", {"query": "RunStore"})


def test_explicit_sdk_config_does_not_replace_legacy_default(tmp_path):
    index(tmp_path)
    pid_path = tmp_path / "server.pid"
    config = server_config(tmp_path, pid_path)
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"knowledge": {"client": "sdk", "command": config.command,
        "args": config.args, "trustedReadOnlyTools": ["knowledge_search", "knowledge_get_chunk"]}}}), encoding="utf-8")
    from corecoder.mcp import _live_clients

    try:
        tools = load_mcp_tools(path)
        assert len(tools) == 2 and all(tool.side_effect == "read" and not tool.requires_confirmation for tool in tools)
    finally:
        for client in _live_clients:
            client.close()
        _live_clients.clear()
    legacy = MCPClient("legacy", config.command, list(config.args))
    try:
        result = json.loads(legacy.call_tool("knowledge_search", {"query": "RunStore"}))
        assert result["hits"]
    finally:
        legacy.close()


def test_server_embedding_budget_survives_requests_and_scopes_chunk_reads(tmp_path):
    root, foreign = tmp_path / "project", tmp_path / "foreign"
    root.mkdir()
    foreign.mkdir()
    snapshot = index(root)
    index(foreign)
    scope = create_scope(root)
    shared_foreign = create_scope(foreign, data_root=scope.data_root)
    with KnowledgeService(shared_foreign, EmbeddingGateway(FixtureEmbeddingProvider(64), BudgetLedger())) as service:
        foreign_snapshot = service.index().snapshot_id
        foreign_chunk = service.search("RunStore", mode="lexical").hits[0].chunk.chunk_id
    config = server_config(root, tmp_path / "server.pid", mode="hybrid")
    config = replace(config, args=(*config.args, "--max-requests", "1"))
    with MCPToolBridge(config) as bridge:
        result = bridge.call_tool("knowledge_search", {"query": "RunStore"})
        assert not result.is_error and result.structured_content["snapshot_id"] == snapshot
        assert result.structured_content["server_budget"]["requests"] == 1
        assert bridge.call_tool("knowledge_search", {"query": "RunStore"}).is_error
        assert bridge.call_tool("knowledge_get_chunk", {"chunk_id": foreign_chunk, "snapshot_id": foreign_snapshot}).is_error
        own_id = result.structured_content["hits"][0]["chunk_id"]
        own = bridge.call_tool("knowledge_get_chunk", {"chunk_id": own_id, "snapshot_id": snapshot})
        assert not own.is_error and own.structured_content["server_budget"]["requests"] == 1


def test_pagination_timeout_cancellation_error_redaction_and_disconnect(tmp_path):
    pid = tmp_path / "paged.pid"
    config = SDKServerConfig("paged", sys.executable, (str(FIXTURES / "paged_mcp.py"), str(pid)), call_timeout=0.3)
    with MCPToolBridge(config) as bridge:
        assert {tool.remote_name for tool in bridge.tools} == {"echo", "slow", "fail", "crash"}
        assert all(tool.requires_confirmation for tool in bridge.tools)  # readOnlyHint is not authority.
        tools = {tool.remote_name: tool for tool in bridge.tools}
        with pytest.raises(ToolError, match="rejected") as failure:
            tools["fail"].execute()
        assert "sk-TEST_SECRET" not in str(failure.value)
        with pytest.raises(ToolError, match="timeout"):
            tools["slow"].execute()
        assert tools["echo"].execute().ok
        marker = pid.with_suffix(".cancelled")
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.01)
        assert marker.exists(), "SDK timeout must notify the running server task"
        marker.unlink()
        cancelled = threading.Event()
        context = ToolExecutionContext(run_id="fixture", tool_call_id="slow", tool_name="slow", cancellation_probe=cancelled.is_set)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(tools["slow"].execute_with_context, {}, context)
            time.sleep(0.05)
            cancelled.set()
            with pytest.raises(ToolError, match="cancelled"):
                future.result(timeout=5)
        assert tools["echo"].execute().ok
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.01)
        assert marker.exists(), "caller cancellation must reach the server task"
        with pytest.raises(ToolError):
            tools["crash"].execute()
        bridge.reconnect()
        assert tools["echo"].execute().ok
        final_pid = int(pid.read_text(encoding="ascii"))
    assert bridge.closed and not process_alive(final_pid)
