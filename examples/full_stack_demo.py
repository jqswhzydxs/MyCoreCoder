"""One offline flow: SDK -> Prompt/LangChain -> RAG/Graph -> memory -> MCP.

The real OpenAI SDK uses an in-process HTTP fixture, never a model endpoint.
SQLite, Chroma, LangChain, LangGraph and MCP stdio are real integrations.
Run: python -m examples.full_stack_demo
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import httpx
from openai import OpenAI

from corecoder.integrations.mcp_sdk_bridge import MCPToolBridge, SDKServerConfig
from corecoder.integrations.model_gateway import ModelGateway
from corecoder.knowledge.embedding_gateway import EmbeddingGateway
from corecoder.knowledge.embeddings import FixtureEmbeddingProvider
from corecoder.knowledge.scope import create_scope
from corecoder.knowledge.service import KnowledgeService
from corecoder.llm import LLM
from corecoder.memory.service import MemoryService
from corecoder.prompts.registry import load_prompt
from corecoder.workflows.budget_ledger import BudgetLedger, WorkflowBudget
from corecoder.workflows.fixtures import FixtureWorkflowBackend
from corecoder.workflows.graph import GraphWorkflow
from examples.knowledge_mcp_client import fetch


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class OfflineAPI:
    """Exercise production SDK request/SSE parsing, without loading credentials."""

    def __init__(self):
        self.observations = []
        self.invalid_reference_sent = False
        self.fixture = FixtureWorkflowBackend()
        self.backend = LLM("full-stack-http-fixture", "unused-fixture-key", "https://offline.invalid/v1")
        self.backend.client.close()
        self.backend.client = OpenAI(api_key="unused-fixture-key", base_url="https://offline.invalid/v1",
            max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(self.respond)))

    def respond(self, request):
        require(request.url.host == "offline.invalid" and request.url.path == "/v1/chat/completions", "unexpected API route")
        body = json.loads(request.content)
        messages = body["messages"]
        payload = json.loads(messages[-1]["content"])
        data, properties = payload["input_data"], payload["output_schema"]["properties"]
        kind = "memory_extract" if "candidates" in properties else "task_plan" if "task_type" in properties else "repo_answer"
        template = load_prompt(kind + ".v2")
        require(messages[0]["content"] == template.text, "actual API prompt differs from versioned template")
        memories = data.get("memories", [item for item in data.get("constraints", [])
                                        if isinstance(item, dict) and item.get("type") == "historical_memory"])
        self.observations.append({"kind": kind, "template_id": template.template_id, "sha256": template.sha256,
                                  "memory_ids": [item["id"] for item in memories], "repair": bool(payload["repair_errors"])})
        if kind == "memory_extract":
            content = json.dumps({"candidates": [{"kind": "preference", "key": "response.language",
                "content": data["source"], "source_ref": data["source_ref"], "evidence_ids": []}]})
        elif kind == "repo_answer" and not self.invalid_reference_sent:
            self.invalid_reference_sent = True
            content = json.dumps({"status": "answered", "answer": "An intentionally unsupported fixture answer.",
                                  "evidence_ids": ["unknown-fixture-reference"], "unknowns": []})
        else:
            content = self.fixture.chat_once(messages, tools=None, timeout_seconds=30, max_tokens=1024).content
        chunk = {"id": "fixture-chat", "object": "chat.completion.chunk", "created": 0, "model": body["model"]}
        events = [
            {**chunk, "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]},
            {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**chunk, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}},
        ]
        stream = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream", "x-request-id": "offline-fixture"},
                              content=stream.encode())

    def gateway(self, ledger):
        return ModelGateway(self.backend, ledger, max_attempts=1)

    def close(self):
        self.backend.client.close()


def run_demo() -> dict:
    api = OfflineAPI()
    checks, turns = {}, []
    try:
        with tempfile.TemporaryDirectory(prefix="corecoder-full-stack-") as directory:
            root = Path(directory)
            (root / "corecoder").mkdir()
            source = root / "corecoder/journal.py"
            source.write_text('class RunStore:\n    """Persist tool execution state before recovery."""\n', encoding="utf-8")
            scope = create_scope(root)
            provider = FixtureEmbeddingProvider(64)

            def knowledge(ledger):
                return KnowledgeService(scope, EmbeddingGateway(provider, ledger))

            with knowledge(BudgetLedger()) as service:
                cold = service.index()
                hot = service.index()
                require(cold.snapshot_id == hot.snapshot_id and hot.embedding_requests == 0, "unchanged index requested embeddings")
                checks["unchanged_index_zero_embedding_requests"] = True

            memory = MemoryService(scope)
            preference = "回答优先使用中文"
            candidates = memory.stage_extraction(api.gateway(BudgetLedger()), source_text=preference,
                source=memory.explicit_source(preference, "initial-preference"), operation_id="extract-preference")
            require(len(candidates) == 1 and candidates[0].status == "candidate", "extraction did not stage a candidate")
            require(not memory.recall("RunStore").memories, "unapproved candidate entered context")
            approved = memory.approve(candidates[0].memory_id)
            require(MemoryService(scope).recall("RunStore").memories == (approved,), "cross-session memory was lost")
            checks["extracted_memory_requires_approval"] = True
            checks["cross_session_memory_recalled"] = True

            def workflow():
                return GraphWorkflow(scope, gateway_factory=api.gateway, knowledge_factory=knowledge,
                    memory_service=MemoryService(scope), trusted_configuration={"demo": "full-stack-v1"})

            def ask(turn_id, expected_memory_ids):
                before, stages = len(api.observations), []
                engine = workflow()
                result = engine.run("RunStore", thread_id="full-stack", turn_id=turn_id, mode="hybrid",
                    budget=WorkflowBudget(max_requests=10, max_tokens=100_000, deadline_seconds=60),
                    on_event=lambda event: stages.append(event["stage"]))
                require(result.workflow_status == "completed" and result.answer_status == "answered", "graph answer failed")
                require(result.result["sources"] and result.result["mode"] == "hybrid", "missing hybrid retrieval evidence")
                for observed in api.observations[before:]:
                    require(observed["memory_ids"] == expected_memory_ids, "stale or missing memory in actual API messages")
                require(stages == ["prepare", "retrieve", "recall", "plan", "answer", "finalize"], "unexpected graph stages")
                count = len(api.observations)
                resumed = workflow().resume(result.workflow_id)
                require(resumed.result == result.result and len(api.observations) == count, "terminal graph replayed a model call")
                turns.append({"turn": turn_id, "stages": stages, "answer_status": result.answer_status,
                              "source_count": len(result.result["sources"]), "budget": result.budget})
                return result

            first = ask("initial", [approved.memory_id])
            require(sum(item["repair"] for item in api.observations) == 1, "unknown evidence ID did not trigger one bounded repair")
            checks["unknown_citation_rejected_and_repaired"] = True
            checks["memory_reaches_planner_and_answer_api"] = True

            newer = MemoryService(scope).remember("Answer in English", key="response.language", operation_id="new-preference")
            require(newer.supersedes == approved.memory_id, "new preference did not supersede old memory")
            ask("updated", [newer.memory_id])
            checks["superseded_memory_excluded_from_api"] = True

            MemoryService(scope).forget(newer.memory_id)
            replay = MemoryService(scope).remember("Answer in English", key="response.language", operation_id="new-preference")
            require(replay.status == "deleted" and replay.content == "", "old operation resurrected a deleted memory")
            source.write_text(source.read_text(encoding="utf-8") + '\n# RunStore recovery uses persisted execution facts.\n', encoding="utf-8")
            with knowledge(BudgetLedger()) as service:
                edited = service.index()
            require(edited.snapshot_id != cold.snapshot_id, "source edit did not publish a new snapshot")
            final = ask("deleted-and-refreshed", [])
            require(final.result["snapshot_id"] == edited.snapshot_id != first.result["snapshot_id"], "graph used old source snapshot")
            raw_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            require(all(item["raw_hash"] == raw_hash for item in final.result["sources"]), "citation hash differs from current source")
            checks["deleted_memory_excluded_and_not_resurrected"] = True
            checks["edited_source_has_current_citations"] = True
            checks["terminal_resume_zero_additional_chat_requests"] = True

            independent = asyncio.run(fetch(root, "RunStore"))
            config = SDKServerConfig("knowledge", sys.executable,
                ("-m", "corecoder", "mcp", "serve-knowledge", "--root", str(root), "--mode", "lexical"),
                trusted_read_only=("knowledge_search", "knowledge_get_chunk"))
            with MCPToolBridge(config) as bridge:
                search = next(tool for tool in bridge.tools if tool.remote_name == "knowledge_search")
                remote = search.execute(query="RunStore")
                require(remote.ok, "MCP bridge search failed")
                graph_ids = {item["chunk_id"] for item in final.result["sources"]}
                independent_ids = {item["chunk_id"] for item in independent["search"]["hits"]}
                remote_ids = {item["chunk_id"] for item in remote.data["hits"]}
                require(graph_ids.issubset(remote_ids) and remote_ids == independent_ids, "MCP and graph cite different chunks")
                require(remote.data["snapshot_id"] == independent["search"]["snapshot_id"] == edited.snapshot_id,
                        "MCP and graph use different snapshots")
                require(remote.data["server_budget"]["requests"] == 0, "local MCP unexpectedly used a model")
            require(bridge.closed, "MCP bridge did not close")
            checks["graph_and_two_mcp_clients_share_sources"] = True
            checks["mcp_bridge_closed"] = True

        return {"mode": "offline_http_fixture", "status": "passed", "live_model_requests": 0,
            "sdk_http_fixture_requests": len(api.observations), "checks": checks, "turns": turns,
            "prompts": {item["template_id"]: item["sha256"] for item in api.observations},
            "limits": "SDK transport and vector/model responses are fixtures; no live semantic quality or language-adherence claim."}
    finally:
        api.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run_demo()
    except Exception as exc:  # noqa: BLE001 - CLI reports never include provider bodies or credentials
        report = {"status": "failed", "error_category": type(exc).__name__}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
