"""Independent official-SDK client for an existing local knowledge index.

Install corecoder[knowledge,mcp], then run:
python -m examples.knowledge_mcp_client --root PROJECT --query RunStore
Lexical mode needs no model credentials and makes no model requests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


async def fetch(root: Path, query: str, *, mode: str = "lexical") -> dict:
    from mcp import Client, StdioServerParameters

    environment = None
    if mode != "lexical":
        from corecoder.knowledge.settings import load_settings

        settings = load_settings({"root": str(root)})
        # SDK stdio uses a minimal environment. Forward only this explicitly
        # selected service's settings/key, never the full parent environment.
        environment = {key: value for key, value in os.environ.items()
                       if key.startswith(("CORECODER_EMBEDDING_", "CORECODER_KNOWLEDGE_"))}
        key_name = settings.embedding.api_key_env
        if key_name in os.environ:
            environment[key_name] = os.environ[key_name]
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "corecoder", "mcp", "serve-knowledge",
        "--root", str(root.resolve()), "--mode", mode], env=environment)
    async with Client(parameters, read_timeout_seconds=30, cache=None) as client:
        definitions = await client.list_tools()
        names = {tool.name for tool in definitions.tools}
        if not {"knowledge_search", "knowledge_get_chunk"}.issubset(names):
            raise RuntimeError("knowledge server did not advertise the required tools")
        search = await client.call_tool("knowledge_search", {"query": query})
        if search.is_error or search.structured_content is None:
            raise RuntimeError("knowledge server rejected the search request")
        result = search.structured_content
        chunks = []
        for hit in result["hits"]:
            response = await client.call_tool("knowledge_get_chunk", {
                "chunk_id": hit["chunk_id"], "snapshot_id": result["snapshot_id"]})
            if response.is_error or response.structured_content is None:
                raise RuntimeError("knowledge source is unavailable or changed after search")
            chunks.append(response.structured_content["chunk"])
        return {"protocol_version": client.protocol_version, "search": result, "chunks": chunks}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--mode", choices=["lexical", "vector", "hybrid"], default="lexical")
    args = parser.parse_args(argv)
    print(json.dumps(asyncio.run(fetch(args.root, args.query, mode=args.mode)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
