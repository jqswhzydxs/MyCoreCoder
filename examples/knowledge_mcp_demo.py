"""Same source through GraphWorkflow, Agent MCP tools and an independent SDK client.

Run: python -m examples.knowledge_mcp_demo
Requires corecoder[knowledge,graph,mcp]. Real SQLite/Chroma, StateGraph and stdio;
fixed embeddings and scripted generation, no model requests or charges.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from corecoder import Agent
from corecoder.demo import ScriptedLLM
from corecoder.integrations.mcp_sdk_bridge import MCPToolBridge, SDKServerConfig
from corecoder.integrations.model_gateway import ModelGateway
from corecoder.knowledge.embedding_gateway import EmbeddingGateway
from corecoder.knowledge.embeddings import FixtureEmbeddingProvider
from corecoder.knowledge.scope import create_scope
from corecoder.knowledge.service import KnowledgeService
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.workflows.budget_ledger import BudgetLedger
from corecoder.workflows.fixtures import FixtureWorkflowBackend
from corecoder.workflows.graph import GraphWorkflow
from examples.knowledge_mcp_client import fetch


def run_demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="corecoder-mcp-demo-") as directory:
        root = Path(directory)
        (root / "corecoder").mkdir()
        (root / "corecoder/journal.py").write_text('class RunStore:\n    """Journal execution facts before recovery."""\n', encoding="utf-8")
        scope = create_scope(root)

        def knowledge(ledger):
            return KnowledgeService(scope, EmbeddingGateway(FixtureEmbeddingProvider(64), ledger))

        with knowledge(BudgetLedger()) as service:
            snapshot = service.index().snapshot_id
        workflow = GraphWorkflow(scope, gateway_factory=lambda ledger: ModelGateway(FixtureWorkflowBackend(), ledger),
                                 knowledge_factory=knowledge, trusted_configuration={"demo": "mcp-sources-v1"})
        graph = workflow.run("RunStore", thread_id="demo", turn_id="ask", mode="lexical")
        assert graph.answer_status == "answered" and graph.result["snapshot_id"] == snapshot
        independent = asyncio.run(fetch(root, "RunStore"))
        assert independent["search"]["snapshot_id"] == snapshot and independent["chunks"]
        config = SDKServerConfig("knowledge", sys.executable,
            ("-m", "corecoder", "mcp", "serve-knowledge", "--root", str(root), "--mode", "lexical"),
            trusted_read_only=("knowledge_search", "knowledge_get_chunk"))
        with MCPToolBridge(config) as bridge:
            search = next(tool for tool in bridge.tools if tool.remote_name == "knowledge_search")
            agent = Agent(ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall("search", search.name, {"query": "RunStore"})]),
                LLMResponse(content="The search result contains current repository evidence.")]),
                tools=[search], permission=Permission())
            agent.run("RunStore")
            assert agent.tool_results[0].ok
            remote = agent.tool_results[0].data
            assert remote["snapshot_id"] == snapshot
            ids = {hit["chunk_id"] for hit in independent["search"]["hits"]}
            assert ids == {hit["chunk_id"] for hit in remote["hits"]}
            assert {source["chunk_id"] for source in graph.result["sources"]}.issubset(ids)
            assert remote["server_budget"]["requests"] == 0
        assert bridge.closed
        return {"mode": "offline_fixture", "snapshot_id": snapshot,
                "protocol_version": independent["protocol_version"], "matching_chunk_ids": sorted(ids),
                "graph_sources": graph.result["sources"], "sdk_bridge_closed": bridge.closed,
                "server_model_requests": remote["server_budget"]["requests"]}


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
