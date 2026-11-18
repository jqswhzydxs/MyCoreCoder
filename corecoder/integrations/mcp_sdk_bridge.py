"""Official SDK stdio tools on a dedicated, explicitly closable asyncio thread."""

from __future__ import annotations

import asyncio
import concurrent.futures
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field

from ..knowledge.models import digest
from ..tools.base import Tool
from ..tools.protocol import ToolError, ToolOutcome, ToolResult


class SDKBridgeError(RuntimeError):
    def __init__(self, category: str):
        self.category = category
        super().__init__("official MCP SDK operation failed: " + category)


@dataclass(frozen=True)
class SDKServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict, repr=False)
    cwd: str | None = None
    mode: str = "auto"
    startup_timeout: float = 30
    call_timeout: float = 30
    trusted_read_only: tuple[str, ...] = ()

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", self.name) or not isinstance(self.command, str) or not self.command:
            raise ValueError("SDK server requires a bounded name and explicit executable")
        if self.mode not in {"auto", "legacy"}:
            raise ValueError("SDK protocol mode must be auto or legacy")
        if any(not math.isfinite(value) or not 0 < value <= 120 for value in (self.startup_timeout, self.call_timeout)):
            raise ValueError("SDK timeouts must be positive and at most 120 seconds")


class MCPToolBridge:
    """One context task owns Client entry/exit on one loop for its full lifetime."""

    def __init__(self, config: SDKServerConfig):
        self.config = config
        self._futures_lock = threading.Lock()
        self._futures: set[concurrent.futures.Future] = set()
        self._tools = []
        self._contracts = {}
        self.protocol_version = ""
        self._start()

    def _start(self):
        self._ready = threading.Event()
        self._closed = False
        self._startup_error = None
        self._loop = None
        self._stop = None
        self._serve_task = None
        self._client = None
        self._thread = threading.Thread(target=self._thread_main, name="corecoder-mcp-sdk-" + self.config.name, daemon=True)
        self._thread.start()
        if not self._ready.wait(self.config.startup_timeout):
            self.close()
            raise SDKBridgeError("startup_timeout")
        if self._startup_error or self._closed:
            self.close()
            raise SDKBridgeError(self._startup_error or "connection_closed")

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._serve_task = loop.create_task(self._serve())
            loop.run_until_complete(self._serve_task)
        except BaseException:  # noqa: BLE001 - SDK task groups may raise exception groups; no bodies enter diagnostics
            self._startup_error = "transport_error"
        finally:
            self._closed = True
            self._ready.set()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _serve(self):
        from mcp import Client, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stop = asyncio.Event()
        parameters = StdioServerParameters(command=self.config.command, args=list(self.config.args),
            env=self.config.env or None, cwd=self.config.cwd)
        # Third-party stderr is untrusted and may contain credentials. The
        # bridge returns content-free errors instead of forwarding raw logs.
        with open(os.devnull, "w", encoding="utf-8") as diagnostics:  # noqa: ASYNC230 - SDK needs the OS null device descriptor
            transport = stdio_client(parameters, errlog=diagnostics)
            async with Client(transport, mode=self.config.mode, read_timeout_seconds=self.config.startup_timeout, cache=None) as client:
                self._client = client
                self.protocol_version = client.protocol_version
                definitions = await self._discover(client)
                contracts = {item.name: digest([item.input_schema, item.output_schema]) for item in definitions}
                if self._contracts and any(contracts.get(name) != identity for name, identity in self._contracts.items()):
                    self._startup_error = "tool_contract_changed"
                    return
                self._contracts = contracts
                self._tools = [SDKTool(self, item) for item in definitions]
                names = [tool.name for tool in self._tools]
                if len(names) != len(set(names)):
                    self._startup_error = "duplicate_tool_name"
                    return
                self._ready.set()
                await self._stop.wait()

    @staticmethod
    async def _discover(client):
        cursor, seen, definitions = None, set(), []
        for _ in range(32):
            page = await client.list_tools(cursor=cursor, cache_mode="refresh")
            definitions.extend(page.tools)
            if len(definitions) > 512 or len({item.name for item in definitions}) != len(definitions):
                raise SDKBridgeError("invalid_tool_listing")
            cursor = page.next_cursor
            if cursor is None:
                return definitions
            if cursor in seen:
                raise SDKBridgeError("pagination_cycle")
            seen.add(cursor)
        raise SDKBridgeError("pagination_limit")

    @property
    def tools(self) -> list:
        return list(self._tools)

    @property
    def name(self):
        return self.config.name

    @property
    def error(self):
        return SDKBridgeError("connection_closed") if self._closed else None

    @property
    def closed(self):
        return self._closed and not self._thread.is_alive()

    async def _call(self, name, arguments, timeout):
        if self._client is None:
            raise SDKBridgeError("connection_closed")
        return await asyncio.wait_for(self._client.call_tool(name, arguments, read_timeout_seconds=timeout + 1), timeout=timeout)

    def call_tool(self, name: str, arguments: dict, *, context=None):
        if self._closed or self._loop is None or self._loop.is_closed():
            raise SDKBridgeError("connection_closed")
        timeout = self.config.call_timeout
        if context is not None:
            if context.cancellation_probe():
                raise SDKBridgeError("cancelled")
            remaining = context.remaining_timeout()
            if remaining is not None:
                timeout = min(timeout, remaining)
        if timeout <= 0:
            raise SDKBridgeError("timeout")
        future = asyncio.run_coroutine_threadsafe(self._call(name, arguments, timeout), self._loop)
        with self._futures_lock:
            self._futures.add(future)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if context is not None and context.cancellation_probe():
                    future.cancel()
                    raise SDKBridgeError("cancelled")
                if time.monotonic() >= deadline:
                    future.cancel()
                    raise SDKBridgeError("timeout")
                try:
                    return future.result(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
                except concurrent.futures.TimeoutError:
                    if future.done():
                        raise SDKBridgeError("timeout") from None
        except SDKBridgeError:
            raise
        except Exception:  # noqa: BLE001 - remote error bodies/exception groups are untrusted
            raise SDKBridgeError("remote_error") from None
        finally:
            with self._futures_lock:
                self._futures.discard(future)

    def close(self):
        with self._futures_lock:
            for future in tuple(self._futures):
                future.cancel()
        if self._loop and not self._loop.is_closed():
            try:
                if self._stop is not None:
                    self._loop.call_soon_threadsafe(self._stop.set)
                elif self._serve_task is not None:
                    self._loop.call_soon_threadsafe(self._serve_task.cancel)
            except RuntimeError:
                pass
        self._thread.join(timeout=8)
        if self._thread.is_alive() and self._loop and not self._loop.is_closed() and self._serve_task:
            self._loop.call_soon_threadsafe(self._serve_task.cancel)
            self._thread.join(timeout=8)
        if self._thread.is_alive():
            raise SDKBridgeError("cleanup_incomplete")
        self._closed = True

    def reconnect(self):
        self.close()
        self._start()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class SDKTool(Tool):
    context_aware = True
    manages_own_timeout = True
    cacheable = False  # Current source hashes must be checked on every call.

    def __init__(self, bridge: MCPToolBridge, definition):
        self.bridge, self.remote_name = bridge, definition.name
        name = "mcp__" + bridge.config.name + "__" + re.sub(r"[^A-Za-z0-9_-]", "_", definition.name)
        self.name = name if len(name) <= 64 else name[:47] + "_" + digest(name)[:16]
        self.description = definition.description or "External MCP tool"
        self.parameters = definition.input_schema
        self.output_schema = definition.output_schema or {"type": "object"}
        self.timeout_seconds = bridge.config.call_timeout
        self.version = "sdk2-" + digest([self.parameters, self.output_schema])[:16]
        # readOnlyHint alone never changes authority. The trusted host may
        # explicitly name its own read-only tools in local configuration.
        trusted = definition.name in bridge.config.trusted_read_only
        self.side_effect = "read" if trusted else "external"
        self.risk = "low" if trusted else "high"
        self.requires_confirmation = not trusted
        self.idempotent = trusted

    def execute(self, **kwargs):
        return self._execute(kwargs)

    def execute_with_context(self, arguments, context):
        return self._execute(arguments, context)

    def _execute(self, arguments, context=None):
        try:
            result = self.bridge.call_tool(self.remote_name, arguments, context=context)
        except SDKBridgeError as exc:
            outcome = {"cancelled": ToolOutcome.CANCELLED, "timeout": ToolOutcome.TIMEOUT}.get(exc.category, ToolOutcome.UNKNOWN_ERROR)
            raise ToolError("Error: MCP tool request " + exc.category + ".", category="mcp_" + exc.category,
                            code="MCP_SDK_FAILED", outcome=outcome, retryable=False) from None
        if result.is_error:
            raise ToolError("Error: MCP server rejected the tool request.", category="mcp_tool_error", code="MCP_TOOL_REJECTED")
        data = result.structured_content
        if data is None:
            data = {"content": [item.model_dump(mode="json") for item in result.content]}
        return ToolResult.success("", self.name, data)
