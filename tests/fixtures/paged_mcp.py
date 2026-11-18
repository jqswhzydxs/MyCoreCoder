"""Official SDK server fixture: pagination, cancellation, errors and disconnects."""

import os
import sys
from pathlib import Path

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

pid_file = Path(sys.argv[1])
pid_file.write_text(str(os.getpid()), encoding="ascii")
tools = [Tool(name=name, description="Offline transport fixture", input_schema={"type": "object", "properties": {}},
              output_schema={"type": "object"}, annotations=ToolAnnotations(read_only_hint=True))
         for name in ("echo", "slow", "fail", "crash")]


async def list_tools(context, params):
    cursor = params.cursor if params else None
    return ListToolsResult(tools=tools[:2] if cursor is None else tools[2:], next_cursor="page-2" if cursor is None else None)


async def call_tool(context, params):
    if params.name == "crash":
        os._exit(1)
    if params.name == "slow":
        try:
            await anyio.sleep(30)
        finally:
            pid_file.with_suffix(".cancelled").write_text("cancelled", encoding="ascii")
    if params.name == "fail":
        return CallToolResult(content=[TextContent(type="text", text="sk-TEST_SECRET_REMOTE_ERROR_123456")], is_error=True)
    return CallToolResult(content=[TextContent(type="text", text="ok")], structured_content={"ok": True})


async def main():
    server = Server("paged-fixture", on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


anyio.run(main)
