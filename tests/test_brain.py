"""The agent loop and permission gate, against a scripted fake Claude and a real in-process MCP node."""

from contextlib import asynccontextmanager

import anthropic
import httpx2
import pytest
from fakes import FakeClaude, build_node, config, text_reply, tool_call
from mcp import Client

from sommus.brain.loop import Brain, Notice, TextDelta, ToolFinished, TurnDone
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy, Tier
from sommus.brain.store import Store


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
        assert result == {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": [{"type": "text", "text": "all quiet"}],
            "is_error": False,
        }


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
        assert result["is_error"] is True and "declined" in result["content"][0]["text"]


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


async def test_request_uses_refusal_fallback_and_sorted_tools(tmp_path):
    async with make_brain(tmp_path, text_reply("Hi.")) as (brain, claude, _):
        await run(brain, "hello", never_confirm)

        request = claude.requests[0]
        assert request["fallbacks"] == "default"
        assert [t["name"] for t in request["tools"]] == sorted(t["name"] for t in request["tools"])


async def test_an_unreachable_node_does_not_stop_the_brain(tmp_path):
    from sommus.config import NodeConfig

    async with NodeHub((NodeConfig("ghost", "sommus.nodes.does_not_exist"),), Policy()) as hub:
        assert "ghost" in hub.unreachable
        assert hub.tools == []  # brain still starts, just with no tools from that node


async def test_the_cache_breakpoint_sits_on_the_stable_prefix(tmp_path):
    """A breakpoint after the user's message would write a new cache entry every turn."""
    async with make_brain(tmp_path, text_reply("Hi."), text_reply("Hi again.")) as (brain, claude, _):
        await run(brain, "hello", never_confirm)
        await run(brain, "hello again", never_confirm)

    for request in claude.requests:
        assert "cache_control" not in request
        assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert request["system"][0]["text"] == brain.system
