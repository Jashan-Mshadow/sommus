"""Scripted fake Claude, a fake MCP node, and a test Config — shared by the test modules."""

from pathlib import Path
from types import SimpleNamespace

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from sommus.config import Config


def usage():
    return SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def text_reply(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", usage=usage())


def tool_call(name, args, id="toolu_1"):
    block = SimpleNamespace(type="tool_use", id=id, name=name, input=args)
    return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage())


class FakeStream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        if isinstance(self.message, Exception):
            raise self.message
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for block in self.message.content:
            if block.type == "text":
                yield SimpleNamespace(
                    type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=block.text)
                )

    async def get_final_message(self):
        return self.message


class FakeClaude:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **request):
        self.requests.append(request | {"messages": list(request["messages"])})
        return FakeStream(self.responses.pop(0))


def build_node():
    calls = []
    node = MCPServer("test-node", log_level="WARNING")

    @node.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def peek() -> str:
        """Read something."""
        calls.append("peek")
        return "all quiet"

    @node.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True), structured_output=False)
    def wipe(target: str) -> str:
        """Destroy something."""
        calls.append(f"wipe {target}")
        return f"wiped {target}"

    @node.tool(structured_output=False)
    def unannotated() -> str:
        """No annotations at all."""
        calls.append("unannotated")
        return "ran"

    return node, calls


def config(tmp_path: Path, overrides=None, ask=True) -> Config:
    return Config(
        name="Sommus",
        user="Jashan",
        model="claude-opus-5",
        effort="medium",
        max_steps=4,
        max_steps_hard=4,
        web_search=False,
        nodes=(),
        overrides=overrides or {},
        data_dir=tmp_path,
        ask_before_destructive=ask,
    )
