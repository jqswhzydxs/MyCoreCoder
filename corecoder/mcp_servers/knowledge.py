"""Read-only, root-bound repository knowledge over the official MCP SDK."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Annotated, Any

from ..knowledge.cli import _service, common_arguments
from ..knowledge.models import KnowledgeError
from ..knowledge.settings import load_settings
from ..workflows.budget_ledger import BudgetExceeded, BudgetLedger, WorkflowBudget


def build_server(settings, *, mode: str | None = None, budget: WorkflowBudget | None = None):
    # SDK imports are confined to an explicitly selected MCP path.
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.shared.exceptions import MCPError
    from mcp.types import INVALID_PARAMS, ToolAnnotations
    from pydantic import Field

    ledger = BudgetLedger(budget or WorkflowBudget(max_requests=200, max_tokens=2_000_000, deadline_seconds=3600))
    async def argument_guard(context, call_next):
        if context.method == "tools/call":
            params = context.params or {}
            allowed = {"knowledge_search": {"query", "top_k", "snapshot_id"},
                       "knowledge_get_chunk": {"chunk_id", "snapshot_id"}}.get(params.get("name"))
            arguments = params.get("arguments", {})
            if allowed is not None and (not isinstance(arguments, dict) or not set(arguments).issubset(allowed)):
                raise MCPError(code=INVALID_PARAMS, message="knowledge tool arguments contain unknown fields")
        return await call_next(context)

    server = MCPServer("corecoder-knowledge", version="1", log_level="WARNING", middleware=[argument_guard])
    retrieval_mode = mode or settings.retrieval_mode
    annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

    def perform(operation):
        try:
            # Each request owns and closes its Chroma handles. All remote
            # query embeddings share this server process's separate ledger.
            gateway = settings.gateway(ledger, required=False) if retrieval_mode != "lexical" else None
            with _service(settings, gateway) as service:
                result = operation(service)
            result["server_budget"] = ledger.snapshot()
            if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
                raise KnowledgeError("response_too_large", "knowledge result exceeds the protocol response limit")
            return result
        except BudgetExceeded:
            raise ToolError("budget_exceeded: knowledge server request budget is exhausted") from None
        except KnowledgeError as exc:
            raise ToolError(exc.category + ": " + str(exc)) from None
        except Exception:  # noqa: BLE001 - no source/provider traceback is sent to MCP diagnostics
            raise ToolError("internal_error: knowledge request could not be completed") from None

    # Use concrete annotations, not deferred local type strings: the SDK builds
    # Pydantic argument models from these signatures when registering tools.
    def knowledge_search(query, top_k=5, snapshot_id=None):
        """Search the configured project. Returned IDs/citations are current source metadata."""
        if (not isinstance(query, str) or not query.strip() or len(query) > 2000
                or isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 10
                or (snapshot_id is not None and (not isinstance(snapshot_id, str) or len(snapshot_id) != 64))):
            raise ToolError("invalid_request: query, top_k or snapshot is outside the supported range")

        def search(service):
            result = service.search(query, top_k=top_k, mode=retrieval_mode, snapshot_id=snapshot_id)
            hits = []
            for hit in result.hits:
                value = hit.to_dict()
                value["summary"] = value.pop("text")[:500]
                value["summary_truncated"] = len(hit.chunk.text) > 500
                hits.append(value)
            return {"snapshot_id": result.snapshot_id, "mode": result.mode, "degraded": list(result.degraded), "hits": hits}

        return perform(search)

    knowledge_search.__annotations__ = {
        "query": Annotated[str, Field(min_length=1, max_length=2000, strict=True)],
        "top_k": Annotated[int, Field(ge=1, le=10, strict=True)],
        "snapshot_id": Annotated[str, Field(pattern="^[a-f0-9]{64}$")] | None,
        "return": dict[str, Any],
    }
    server.tool(annotations=annotations, structured_output=True)(knowledge_search)

    def knowledge_get_chunk(chunk_id, snapshot_id):
        """Get one current chunk by published snapshot and ID inside the configured project."""
        if any(not isinstance(value, str) or len(value) != 64 for value in (chunk_id, snapshot_id)):
            raise ToolError("invalid_request: chunk and snapshot IDs must be generated SHA-256 identifiers")

        def get(service):
            chunk = service.get_chunk(chunk_id, snapshot_id)
            if len(chunk.text) > 12000:
                raise KnowledgeError("response_too_large", "chunk exceeds the protocol body limit")
            return {"snapshot_id": snapshot_id, "chunk": chunk.to_dict()}

        return perform(get)

    knowledge_get_chunk.__annotations__ = {
        "chunk_id": Annotated[str, Field(pattern="^[a-f0-9]{64}$")],
        "snapshot_id": Annotated[str, Field(pattern="^[a-f0-9]{64}$")], "return": dict[str, Any],
    }
    server.tool(annotations=annotations, structured_output=True)(knowledge_get_chunk)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(prog="corecoder mcp serve-knowledge", description=__doc__)
    common_arguments(parser)
    parser.set_defaults(max_requests=200, max_tokens=2_000_000, deadline=3600)
    parser.add_argument("--mode", choices=["lexical", "vector", "hybrid"])
    args = parser.parse_args(argv)
    # Only the SDK writes protocol data to stdout. Startup errors use stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        settings = load_settings(vars(args))
        server = build_server(settings, mode=args.mode, budget=WorkflowBudget(args.max_requests, args.max_tokens, args.deadline, args.max_cost))
        server.run(transport="stdio")
        return 0
    except ImportError:
        print('configuration_error: knowledge MCP requires corecoder[knowledge,mcp]', file=sys.stderr)
        return 2
    except (KnowledgeError, ValueError):
        print("configuration_error: knowledge MCP startup configuration is invalid", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
