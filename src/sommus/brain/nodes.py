"""Connects to every node (an MCP server) and routes tool calls to the right one."""

from __future__ import annotations

import json
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import Client, StdioServerParameters, types
from mcp.client.stdio import get_default_environment

from sommus.brain.permissions import Policy, Tier
from sommus.config import NodeConfig

MAX_RESULT_CHARS = 10_000


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
    def __init__(self, nodes: tuple[NodeConfig, ...], policy: Policy):
        self._node_configs = nodes
        self._policy = policy
        self._clients: dict[str, Client] = {}
        self._tools: dict[str, NodeTool] = {}
        self.unreachable: dict[str, str] = {}  # node name → why
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> NodeHub:
        try:
            for node in self._node_configs:
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
        await self._stack.aclose()

    async def add(self, name: str, client: Client) -> None:
        """Connect a node. Public so tests can attach in-process servers."""
        await self._stack.enter_async_context(client)
        self._clients[name] = client
        for tool in (await client.list_tools()).tools:
            if tool.name in self._tools:
                raise ValueError(f"Tool '{tool.name}' is defined by both '{self._tools[tool.name].node}' and '{name}'.")
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
