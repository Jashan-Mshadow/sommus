"""Connects to every node (an MCP server) and routes tool calls to the right one."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client, StdioServerParameters, types
from mcp.client.stdio import get_default_environment
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from sommus.brain.permissions import Policy, Tier
from sommus.config import ROOT, NodeConfig

MAX_RESULT_CHARS = 10_000
REMOTE_WAIT_SECONDS = 4.0  # how long a tool call waits for an asleep Mac before saying so


class RemoteNode:
    """A node on another machine — the Mac's tools, seen from the server — over MCP on the tailnet.

    One task owns the connection, because an MCP client must close in the task that opened it, and it
    reopens the connection after the Mac sleeps or `sommus node` restarts. The tool list is cached on
    disk, so while the laptop is asleep the brain still knows its tools and can say "the Mac is asleep"
    instead of acting as if they never existed.
    """

    def __init__(self, name: str, url: str, token: str, cache: Path):
        self.name, self.url, self.token, self.cache = name, url, token, cache
        self.client: Client | None = None
        self.error = "not connected yet"
        self._ready = asyncio.Event()
        self._lost = asyncio.Event()
        self._retry = asyncio.Event()

    def cached_tools(self) -> list[types.Tool]:
        with contextlib.suppress(OSError, ValueError):
            return [types.Tool.model_validate(t) for t in json.loads(self.cache.read_text())]
        return []

    async def run(self, on_tools: Callable[[str, list[types.Tool]], None]) -> None:
        delay = 5.0
        while True:
            try:
                headers = {"Authorization": f"Bearer {self.token}"}
                http = create_mcp_http_client(headers=headers, timeout=httpx2.Timeout(8.0, read=300.0))
                async with http, Client(streamable_http_client(self.url, http_client=http)) as client:
                    tools = (await client.list_tools()).tools
                    with contextlib.suppress(OSError):
                        self.cache.parent.mkdir(parents=True, exist_ok=True)
                        self.cache.write_text(json.dumps([t.model_dump(mode="json") for t in tools]))
                    on_tools(self.name, tools)
                    self.client, self.error, delay = client, "", 5.0
                    self._lost.clear()
                    self._ready.set()
                    await self._lost.wait()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            finally:
                self.client = None
                self._ready.clear()
            self._retry.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._retry.wait(), delay)
            delay = min(delay * 2, 60.0)

    async def connected(self, wait: float = REMOTE_WAIT_SECONDS) -> Client | None:
        if self.client:
            return self.client
        self._retry.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), wait)
        return self.client

    def drop(self) -> None:
        """A call failed: the connection is probably gone (the Mac slept). Reconnect."""
        self.client = None  # so the retry waits for a fresh connection instead of reusing the dead one
        self._ready.clear()
        self._lost.set()


@dataclass(frozen=True)
class ToolOutput:
    """What a tool produced: API content blocks, a text summary for logs, and whether it failed."""

    blocks: list[dict[str, Any]]
    text: str
    is_error: bool

    @classmethod
    def failure(cls, message: str) -> ToolOutput:
        return cls([{"type": "text", "text": message}], message, True)


@dataclass(frozen=True)
class NodeTool:
    node: str
    tool: types.Tool
    tier: Tier


class NodeHub:
    def __init__(self, nodes: tuple[NodeConfig, ...], policy: Policy, cache_dir: Path | None = None):
        self._node_configs = nodes
        self._policy = policy
        self._clients: dict[str, Client] = {}
        self._tools: dict[str, NodeTool] = {}
        self._remote: dict[str, RemoteNode] = {}
        self._tasks: list[asyncio.Task] = []
        self._cache_dir = cache_dir or ROOT / "data" / "nodes"
        self.unreachable: dict[str, str] = {}  # node name → why
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> NodeHub:
        try:
            for node in self._node_configs:
                if node.url:
                    await self._add_remote(node)
                    continue
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", node.module],
                    env={**get_default_environment(), **node.env},
                )
                try:
                    await self.add(node.name, Client(params))
                except Exception as e:
                    # A device that's off or unplugged must not stop the brain: the rest
                    # of Sommus keeps working, and it says which node is missing.
                    self.unreachable[node.name] = str(e) or type(e).__name__
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        await self._stack.aclose()

    async def _add_remote(self, node: NodeConfig) -> None:
        remote = RemoteNode(node.name, node.url or "", os.environ.get("SOMMUS_NODE_TOKEN", ""),
                            self._cache_dir / f"{node.name}.json")  # fmt: skip
        self._remote[node.name] = remote
        self._register(node.name, remote.cached_tools())
        self._tasks.append(asyncio.create_task(remote.run(self._register)))
        if await remote.connected(wait=6.0) is None:
            self.unreachable[node.name] = f"the Mac isn't reachable yet ({remote.error}); it connects when it wakes"

    def _register(self, name: str, tools: list[types.Tool]) -> None:
        """(Re)declare a remote node's tools: from the cache at start, then from the live list."""
        for tool in tools:
            owner = self._tools.get(tool.name)
            if owner and owner.node != name:
                continue  # a local node already defines it; the first one wins
            self._tools[tool.name] = NodeTool(name, tool, self._policy.tier_for(tool.name, tool.annotations))
        if tools and name in self._remote:
            self.unreachable.pop(name, None)

    async def call_on(self, node: str, tool_name: str, arguments: dict[str, Any]) -> ToolOutput:
        """Run a tool on one particular remote node, even when a local node shadows its name (the server
        copying the Mac's contacts through the Mac's own list_contacts)."""
        remote = self._remote.get(node)
        if remote is None:
            return ToolOutput.failure(f"No remote node '{node}'.")
        result = await self._call_remote(remote, tool_name, arguments)
        if isinstance(result, ToolOutput):
            return result
        text = "\n".join(b.text for b in result.content if isinstance(b, types.TextContent))
        return ToolOutput([{"type": "text", "text": text}], text, bool(result.is_error))

    def reachable(self, node: str) -> bool:
        remote = self._remote.get(node)
        return remote is None or remote.client is not None

    def node_of(self, tool_name: str) -> str | None:
        found = self._tools.get(tool_name)
        return found.node if found else None

    async def add(self, name: str, client: Client) -> None:
        """Connect a node. Public so tests can attach in-process servers."""
        await self._stack.enter_async_context(client)
        self._clients[name] = client
        for tool in (await client.list_tools()).tools:
            owner = self._tools.get(tool.name)
            # A tool on this machine beats a Mac tool of the same name: the server's own contacts work while the
            # Mac sleeps. Two local nodes defining one tool is still a mistake.
            if owner and owner.node not in self._remote:
                raise ValueError(f"Tool '{tool.name}' is defined by both '{owner.node}' and '{name}'.")
            self._tools[tool.name] = NodeTool(name, tool, self._policy.tier_for(tool.name, tool.annotations))

    @property
    def tools(self) -> list[NodeTool]:
        return sorted(self._tools.values(), key=lambda t: t.tool.name)

    def api_tools(self, core: tuple[str, ...] | set[str] = ()) -> list[dict[str, Any]]:
        """Tool definitions for the API, sorted so the prompt cache stays warm.

        When a core set is given, every other tool is marked defer_loading: it stays out of
        the prompt until tool search finds it, which is most of what a request costs.
        """
        tools = []
        for t in self.tools:
            if t.tier is Tier.BLOCKED:
                continue
            definition = {
                "name": t.tool.name,
                "description": t.tool.description or "",
                "input_schema": t.tool.input_schema,
            }
            if core and t.tool.name not in core:
                definition["defer_loading"] = True
            tools.append(definition)
        return tools

    def tier(self, tool_name: str) -> Tier | None:
        found = self._tools.get(tool_name)
        return found.tier if found else None

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> ToolOutput:
        """Run a tool on whichever node owns it."""
        found = self._tools.get(tool_name)
        if found is None:
            return ToolOutput.failure(f"Unknown tool '{tool_name}'.")
        if found.node in self._remote:
            result = await self._call_remote(self._remote[found.node], tool_name, arguments)
            if isinstance(result, ToolOutput):
                return result
        else:
            try:
                result = await self._clients[found.node].call_tool(tool_name, arguments)
            except Exception as e:  # a crashed or disconnected node must not crash the brain
                return ToolOutput.failure(f"The {found.node} node failed: {e}")

        blocks: list[dict[str, Any]] = []
        texts: list[str] = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                texts.append(block.text)
            elif isinstance(block, types.ImageContent):
                # Passed through as an image block, so the model actually sees it.
                blocks.append(
                    {"type": "image", "source": {"type": "base64", "media_type": block.mime_type, "data": block.data}}
                )
                texts.append(f"[{block.mime_type} image]")
        text = "\n".join(texts)
        if not text and result.structured_content is not None:
            text = json.dumps(result.structured_content)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n[truncated]"
        text = text or "(no output)"
        if not blocks or any(t for t in texts if not t.startswith("[")):
            blocks.insert(0, {"type": "text", "text": text})
        return ToolOutput(blocks, text, bool(result.is_error))

    async def _call_remote(self, remote: RemoteNode, tool_name: str, arguments: dict[str, Any]):
        asleep = ToolOutput.failure(
            f"{tool_name} runs on Jashan's MacBook, which isn't reachable right now (asleep, lid closed or "
            "offline). Tell him it'll work once the laptop is awake; don't try another tool for it."
        )
        for _ in range(2):
            client = await remote.connected()
            if client is None:
                return asleep
            try:
                return await client.call_tool(tool_name, arguments)
            except Exception as e:
                remote.drop()  # the Mac slept or its node server restarted: reconnect, then try once more
                remote.error = f"{type(e).__name__}: {e}"
        return asleep
