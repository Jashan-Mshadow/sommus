"""The agent loop and permission gate, against a scripted fake Claude and a real in-process MCP node."""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from sommus.brain.loop import Brain, Notice, TextDelta, ToolFinished, TurnDone
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy, Tier
from sommus.brain.store import Store
from sommus.config import Config

# ------------------------------------------------------------------ fakes


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
        nodes=(),
        overrides=overrides or {},
        data_dir=tmp_path,
        ask_before_destructive=ask,
    )


@asynccontextmanager
async def make_brain(tmp_path, *responses, overrides=None, ask=True):
    """Hub + brain in the test's own task (MCP clients must close in the task that opened them)."""
    node, calls = build_node()
    async with NodeHub((), Policy(overrides or {})) as hub:
        await hub.add("test", Client(node))
        claude = FakeClaude(*responses)
        yield Brain(config(tmp_path, overrides, ask), hub, Store(tmp_path / "test.db"), client=claude), claude, calls


async def run(brain, text, confirm):
    return [event async for event in brain.handle(text, confirm)]


async def never_confirm(name, args):
    pytest.fail(f"confirm should not be called for {name}")


# ------------------------------------------------------------------ tests


async def test_unannotated_tools_default_to_destructive(tmp_path):
    async with make_brain(tmp_path) as (brain, _, _):
        assert brain.hub.tier("peek") is Tier.READ
        assert brain.hub.tier("wipe") is Tier.DESTRUCTIVE
        assert brain.hub.tier("unannotated") is Tier.DESTRUCTIVE


async def test_read_tool_runs_without_asking(tmp_path):
    async with make_brain(tmp_path, tool_call("peek", {}), text_reply("All quiet.")) as (brain, claude, calls):
        events = await run(brain, "anything happening?", never_confirm)

        assert calls == ["peek"]
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["All quiet."]
        assert isinstance(events[-1], TurnDone) and events[-1].steps == 2
        result = claude.requests[1]["messages"][-1]["content"][0]
        assert result == {"type": "tool_result", "tool_use_id": "toolu_1", "content": "all quiet", "is_error": False}


async def test_declined_destructive_tool_never_runs(tmp_path):
    asked = []

    async def decline(name, args):
        asked.append((name, args))
        return False

    async with make_brain(tmp_path, tool_call("wipe", {"target": "disk"}), text_reply("Okay, I won't.")) as (
        brain,
        claude,
        calls,
    ):
        events = await run(brain, "wipe the disk", decline)

        assert asked == [("wipe", {"target": "disk"})]
        assert calls == []
        finished = next(e for e in events if isinstance(e, ToolFinished))
        assert finished.decision == "declined" and finished.is_error
        result = claude.requests[1]["messages"][-1]["content"][0]
        assert result["is_error"] is True and "declined" in result["content"]


async def test_full_permission_mode_runs_destructive_tools_without_asking(tmp_path):
    async with make_brain(tmp_path, tool_call("wipe", {"target": "cache"}), text_reply("Done."), ask=False) as (
        brain,
        _,
        calls,
    ):
        await run(brain, "wipe the cache", never_confirm)
        assert calls == ["wipe cache"]
        assert "full permission" in brain.system


async def test_approved_destructive_tool_runs(tmp_path):
    async def approve(name, args):
        return True

    async with make_brain(tmp_path, tool_call("wipe", {"target": "cache"}), text_reply("Done.")) as (brain, _, calls):
        await run(brain, "wipe the cache", approve)
        assert calls == ["wipe cache"]


async def test_blocked_tool_is_hidden_and_refused(tmp_path):
    async with make_brain(
        tmp_path, tool_call("wipe", {"target": "disk"}), text_reply("Can't."), overrides={"wipe": Tier.BLOCKED}
    ) as (brain, claude, calls):
        await run(brain, "wipe the disk", never_confirm)

        assert "wipe" not in [t["name"] for t in claude.requests[0]["tools"]]
        assert calls == []


async def test_max_steps_stops_a_runaway_loop(tmp_path):
    async with make_brain(tmp_path, *[tool_call("peek", {}, id=f"toolu_{i}") for i in range(4)]) as (brain, _, calls):
        events = await run(brain, "keep peeking", never_confirm)

        assert len(calls) == 4
        assert any(isinstance(e, Notice) and "Stopped after 4 steps" in e.text for e in events)


async def test_api_error_rolls_back_the_turn(tmp_path):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=request), body=None)
    async with make_brain(tmp_path, text_reply("Hi."), error) as (brain, _, _):
        await run(brain, "hello", never_confirm)
        assert len(brain.messages) == 2
        events = await run(brain, "hello again", never_confirm)

        assert len(brain.messages) == 2  # the failed turn left no trace in context
        assert any(isinstance(e, Notice) and "API key" in e.text for e in events)


async def test_turns_and_tool_calls_are_logged(tmp_path):
    async with make_brain(tmp_path, tool_call("peek", {}), text_reply("Quiet.")) as (brain, _, _):
        await run(brain, "status?", never_confirm)

        db = brain.store._db
        assert db.execute("SELECT user_text, reply, status FROM turns").fetchall() == [("status?", "Quiet.", "ok")]
        assert db.execute("SELECT tool, tier, decision FROM tool_calls").fetchall() == [("peek", "read", "ran")]
        count, spent = brain.store.cost_today()
        assert count == 1 and spent > 0


async def test_request_uses_caching_and_refusal_fallback(tmp_path):
    async with make_brain(tmp_path, text_reply("Hi.")) as (brain, claude, _):
        await run(brain, "hello", never_confirm)

        request = claude.requests[0]
        assert request["cache_control"] == {"type": "ephemeral"}
        assert request["fallbacks"] == "default"
        assert [t["name"] for t in request["tools"]] == sorted(t["name"] for t in request["tools"])
