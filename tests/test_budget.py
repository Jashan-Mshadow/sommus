"""Monthly spend guard: warn near the cap, step down to the cheap model past it."""

from dataclasses import replace
from datetime import datetime

from fakes import FakeModel, build_node, config, text_reply
from mcp import Client

from sommus.brain import budget
from sommus.brain.loop import Brain, Notice
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store

LIMIT = budget.Budget(monthly_usd=10.0, warn_at=0.8, cheap_at=0.9, cheap_model="claude-haiku-4-5")


def test_choose_by_share_of_the_month():
    assert budget.choose(LIMIT, "claude-sonnet-5", 3.0) == budget.Decision("claude-sonnet-5", 3.0)
    warn = budget.choose(LIMIT, "claude-sonnet-5", 8.2)
    assert warn.model == "claude-sonnet-5" and "$8.20 of $10" in warn.notice
    cheap = budget.choose(LIMIT, "claude-sonnet-5", 9.1)
    assert cheap.model == "claude-haiku-4-5" and "cheaper model" in cheap.notice


def test_haiku_gets_only_options_it_accepts():
    adaptive = {"type": "adaptive"}
    assert budget.request_options("claude-sonnet-5", "low", adaptive) == {
        "output_config": {"effort": "low"},
        "thinking": adaptive,
    }
    assert budget.request_options("claude-haiku-4-5", "low", adaptive) == {}
    assert budget.request_options("claude-haiku-4-5", "low", {"type": "disabled"}) == {"thinking": {"type": "disabled"}}


def _spent(store: Store, usd: float) -> None:
    store._db.execute(
        "INSERT INTO turns (started_at, user_text, status, model, cost_usd) VALUES (?, 'earlier', 'ok', 'x', ?)",
        (datetime.now().isoformat(timespec="seconds"), usd),
    )
    store._db.commit()


async def _brain(tmp_path, spent: float, replies: int):
    store = Store(tmp_path / "t.db")
    _spent(store, spent)
    node, _ = build_node()
    hub = NodeHub((), Policy({}))
    await hub.__aenter__()
    await hub.add("test", Client(node))
    model = FakeModel(*[text_reply("Hi.") for _ in range(replies)])
    brain = Brain(replace(config(tmp_path), model="claude-sonnet-5"), hub, store, client=model)
    brain.budget = LIMIT
    return brain, model, hub


async def test_normal_month_uses_the_main_model_silently(tmp_path):
    brain, model, hub = await _brain(tmp_path, 2.0, 1)
    events = [e async for e in brain.handle("hello", None)]
    await hub.__aexit__(None, None, None)
    assert not [e for e in events if isinstance(e, Notice)]
    assert model.requests[0]["model"] == "claude-sonnet-5"


async def test_near_the_cap_switches_model_and_says_so_once_a_day(tmp_path):
    brain, model, hub = await _brain(tmp_path, 9.5, 2)
    first = [e async for e in brain.handle("hello", None)]
    second = [e async for e in brain.handle("hello again", None)]
    await hub.__aexit__(None, None, None)
    assert [e.text for e in first if isinstance(e, Notice)] == [budget.choose(LIMIT, "x", 9.5).notice]
    assert not [e for e in second if isinstance(e, Notice)]
    assert all(r["model"] == "claude-haiku-4-5" for r in model.requests)
    assert "output_config" not in model.requests[0] and "thinking" not in model.requests[0]
    assert "fallbacks" not in model.requests[0]
