"""The agent loop and permission gate, against a scripted fake model and a real in-process MCP node."""

from contextlib import asynccontextmanager
from dataclasses import replace

import anthropic
import httpx2
import pytest
from fakes import FakeModel, build_node, config, text_reply, tool_call
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
        model = FakeModel(*responses)
        yield Brain(config(tmp_path, overrides, ask), hub, Store(tmp_path / "test.db"), client=model), model, calls


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
    async with make_brain(tmp_path, tool_call("peek", {}), text_reply("All quiet.")) as (brain, model, calls):
        events = await run(brain, "anything happening?", never_confirm)

        assert calls == ["peek"]
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["All quiet."]
        assert isinstance(events[-1], TurnDone) and events[-1].steps == 2
        result = model.requests[1]["messages"][-1]["content"][0]
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
        model,
        calls,
    ):
        events = await run(brain, "wipe the disk", decline)

        assert asked == [("wipe", {"target": "disk"})]
        assert calls == []
        finished = next(e for e in events if isinstance(e, ToolFinished))
        assert finished.decision == "declined" and finished.is_error
        result = model.requests[1]["messages"][-1]["content"][0]
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
    ) as (brain, model, calls):
        await run(brain, "wipe the disk", never_confirm)

        assert "wipe" not in [t["name"] for t in model.requests[0]["tools"]]
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
    async with make_brain(tmp_path, text_reply("Hi.")) as (brain, model, _):
        await run(brain, "hello", never_confirm)

        request = model.requests[0]
        assert request["fallbacks"] == "default"
        assert [t["name"] for t in request["tools"]] == sorted(t["name"] for t in request["tools"])


async def test_an_unreachable_node_does_not_stop_the_brain(tmp_path):
    from sommus.config import NodeConfig

    async with NodeHub((NodeConfig("ghost", "sommus.nodes.does_not_exist"),), Policy()) as hub:
        assert "ghost" in hub.unreachable
        assert hub.tools == []  # brain still starts, just with no tools from that node


async def test_the_cache_breakpoint_sits_on_the_stable_prefix(tmp_path):
    """A breakpoint after the user's message would write a new cache entry every turn."""
    async with make_brain(tmp_path, text_reply("Hi."), text_reply("Hi again.")) as (brain, model, _):
        await run(brain, "hello", never_confirm)
        await run(brain, "hello again", never_confirm)

    for request in model.requests:
        assert "cache_control" not in request
        assert request["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert request["system"][0]["text"] == brain.system


async def test_only_the_newest_screenshot_stays_in_context(tmp_path):
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "x"}}
    async with make_brain(tmp_path) as (brain, _, _):
        brain.messages = [
            {"role": "user", "content": [{"type": "tool_result", "content": [dict(image)]}]},
            {"role": "user", "content": [{"type": "tool_result", "content": [dict(image)]}]},
            {"role": "user", "content": [{"type": "tool_result", "content": [dict(image)]}]},
        ]
        brain._prune_images()

        kinds = [m["content"][0]["content"][0]["type"] for m in brain.messages]
        assert kinds == ["text", "text", "image"]  # only the last one survives


async def test_it_takes_more_steps_rather_than_abandoning_a_task(tmp_path):
    """The soft budget extends itself; only the hard ceiling stops the turn."""
    node, calls = build_node()
    async with NodeHub((), Policy()) as hub:
        await hub.add("test", Client(node))
        cfg = replace(config(tmp_path), max_steps=2, max_steps_hard=12)
        model = FakeModel(*[tool_call("peek", {}, id=f"toolu_{i}") for i in range(5)], text_reply("Done."))
        brain = Brain(cfg, hub, Store(tmp_path / "t.db"), client=model)

        events = [e async for e in brain.handle("keep peeking", never_confirm)]

        assert len(calls) == 5  # would have stopped at 2 before
        assert any(isinstance(e, Notice) and "more steps to finish" in e.text for e in events)
        assert isinstance(events[-1], TurnDone)


async def test_it_is_warned_before_the_budget_runs_out(tmp_path):
    node, _ = build_node()
    async with NodeHub((), Policy()) as hub:
        await hub.add("test", Client(node))
        cfg = replace(config(tmp_path), max_steps=3, max_steps_hard=3)
        model = FakeModel(tool_call("peek", {}), tool_call("peek", {}, id="toolu_2"), text_reply("Done."))
        brain = Brain(cfg, hub, Store(tmp_path / "t.db"), client=model)

        [e async for e in brain.handle("peek twice", never_confirm)]

        warnings = [
            block["text"]
            for request in model.requests
            for message in request["messages"]
            if isinstance(message["content"], list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        assert any("steps left in this budget" in w for w in warnings)


async def test_only_core_tools_are_loaded_up_front(tmp_path):
    async with make_brain(tmp_path, text_reply("Hi.")) as (brain, model, _):
        brain.cfg = replace(brain.cfg, core_tools=("peek",))
        await run(brain, "hello", never_confirm)

    tools = {t["name"]: t for t in model.requests[0]["tools"]}
    assert "tool_search_tool_bm25" in tools  # the rest are found on demand
    assert "defer_loading" not in tools["peek"]
    assert tools["wipe"]["defer_loading"] is True


async def test_nothing_is_deferred_without_a_core_set(tmp_path):
    async with make_brain(tmp_path, text_reply("Hi.")) as (brain, model, _):
        await run(brain, "hello", never_confirm)
    names = [t["name"] for t in model.requests[0]["tools"]]
    assert "tool_search_tool_bm25" not in names
    assert not any(t.get("defer_loading") for t in model.requests[0]["tools"])


async def test_only_the_newest_tool_results_carry_a_cache_breakpoint(tmp_path):
    async with make_brain(
        tmp_path, tool_call("peek", {}), tool_call("peek", {}, id="toolu_2"), text_reply("Done.")
    ) as (brain, model, _):
        await run(brain, "peek twice", never_confirm)

    final = model.requests[-1]["messages"]
    marked = [
        i
        for i, m in enumerate(final)
        if isinstance(m["content"], list)
        for b in m["content"]
        if isinstance(b, dict) and "cache_control" in b
    ]
    assert marked == [len(final) - 1]  # exactly one, on the latest results
    assert model.requests[0]["system"][0]["cache_control"]["ttl"] == "1h"


async def test_old_turns_are_dropped_beyond_the_history_window(tmp_path):
    async with make_brain(tmp_path, *[text_reply(f"reply {i}") for i in range(5)]) as (brain, _, _):
        brain.cfg = replace(brain.cfg, history_turns=2)
        for i in range(5):
            await run(brain, f"message {i}", never_confirm)

        user_texts = [m["content"] for m in brain.messages if m["role"] == "user"]
        assert len(user_texts) == 2
        assert "message 4" in user_texts[-1] and "message 3" in user_texts[0]


def test_node_cost_trailers_are_read_and_stripped():
    from sommus.brain.loop import split_cost

    assert split_cost("Sunny, 19°C.\n[cost:0.01234]") == ("Sunny, 19°C.", 0.01234)
    assert split_cost("no trailer here") == ("no trailer here", 0.0)


async def test_finished_turns_keep_only_a_stub_of_their_tool_output(tmp_path):
    long_output = "contact " * 400
    async with make_brain(tmp_path, text_reply("Next.")) as (brain, model, _):
        brain.messages = [
            {"role": "user", "content": "[Mon]\nlist my contacts"},
            {"role": "assistant", "content": "calling"},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": long_output}]}
                ],
            },
            {"role": "assistant", "content": "Here they are."},
        ]
        await run(brain, "thanks", never_confirm)

    sent = model.requests[0]["messages"][2]["content"][0]["content"][0]["text"]
    assert sent.endswith("[trimmed]") and len(sent) < 400


async def test_single_tool_model_turns_are_listed_as_fast_path_candidates(tmp_path):
    async with make_brain(tmp_path, tool_call("peek", {}), text_reply("All quiet."), text_reply("Hello!")) as (
        brain,
        _,
        _,
    ):
        await run(brain, "Anything happening?", never_confirm)  # one tool: a candidate
        await run(brain, "hi there", never_confirm)  # no tool: not a candidate

        rows = brain.store.fast_path_candidates()
        assert [(tool, text, count) for tool, text, count, _ in rows] == [("peek", "anything happening?", 1)]
