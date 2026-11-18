"""2026-11-18 review round: one greeting a day, nothing spoken in class, a muted Mac asked once, supplement
back-off, and where each request came from in the activity log."""

from datetime import date, datetime, timedelta

import pytest
from test_announce import NOON, ZONE, FakeHub, make_herald, server  # noqa: F401

from sommus import activity, supplements
from sommus.brain import campus
from sommus.brain.loop import Usage
from sommus.brain.nodes import ToolOutput
from sommus.brain.store import Store

TUE = datetime(2026, 10, 6, tzinfo=ZONE)  # the test schedule: ECE 105 8:30-9:20, MATH 117 tutorial 10:30-11:20


def at(h, m, day=6):
    return TUE.replace(day=day, hour=h, minute=m)


def spoken(server):  # noqa: F811
    return [a["text"] for _n, t, a in server.hub_fake.calls if t == "announce"]


# ---- the greeting: made once, retried as is, dropped when stale ----


async def test_a_busy_greeting_is_made_once_and_said_when_free(server, monkeypatch):  # noqa: F811
    sched, made, free = campus.cached(None), [], False
    monkeypatch.setattr("sommus.brief.spoken_morning", lambda *a, **k: made.append(1) or f"Hello {len(made)}")

    async def busy(_now):
        return "" if free else "a thing"

    server.herald.busy = busy
    for minute in (40, 55):
        server._greet_tried = float("-inf")
        await server.greet(sched, at(7, minute), at(7, 0))
    assert len(made) == 1 and spoken(server) == []
    free = True
    server._greet_tried = float("-inf")
    await server.greet(sched, at(8, 5), at(7, 0))
    assert spoken(server) == ["Hello 1"] and len(made) == 1 and server.state["greeted"] == "2026-10-06"


async def test_a_stale_greeting_is_dropped_once_the_first_class_starts(server):  # noqa: F811
    async def busy(_now):
        return "x"

    server.herald.busy = busy
    sched = campus.cached(None)
    await server.greet(sched, at(8, 0), at(7, 0))
    assert server.state.get("greeting")
    await server.greet(sched, at(8, 31), at(7, 0))  # ECE 105 started at 8:30
    assert server.state["greeted"] == "2026-10-06" and "greeting" not in server.state
    assert spoken(server) == []


async def test_a_muted_mac_is_told_to_telegram_once_and_left_alone_until_unmuted(server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(server, "tell", server.herald.tell)
    server.herald.hub = hub = FakeHub(refuse="Didn't say it: the Mac is muted.")
    records = []
    server.record_said = lambda *a: records.append(a)
    muted_call = hub.call_on

    async def call_on(node, tool, args):
        if tool == "get_volume":
            hub.calls.append((node, tool, args))
            return ToolOutput([], "Volume 0% (muted)." if hub.refuse else "Volume 40%.", False)
        return await muted_call(node, tool, args)

    hub.call_on = call_on
    sched = campus.cached(None)
    for minute in (40, 50, 59):
        server._greet_tried = float("-inf")
        await server.greet(sched, at(7, minute), at(7, 0))
    assert len([c for c in hub.calls if c[1] == "announce"]) == 1 and len(server.told) == 1
    assert records[-1][2] == "telegram (muted)"
    hub.refuse = ""  # unmuted
    server._greet_tried = float("-inf")
    await server.greet(sched, at(8, 5), at(7, 0))
    assert len([c for c in hub.calls if c[1] == "announce"]) == 2 and server.state["greeted"] == "2026-10-06"


# ---- quiet in class ----


@pytest.mark.parametrize("hour, minute", [(10, 30), (10, 31), (10, 45), (11, 0), (11, 18), (11, 19)])
async def test_nothing_is_spoken_on_speakers_anywhere_inside_a_class(server, hour, minute):  # noqa: F811
    server.herald.busy = server.busy
    assert not await server.herald.deliver("Written.", "Spoken.", at(hour, minute))
    assert spoken(server) == [] and server.told == ["Written."]
    assert server.herald.outcome == "busy"


@pytest.mark.parametrize("hour, minute", [(10, 29), (11, 20)])
async def test_the_minutes_around_a_class_are_not_busy(server, hour, minute):  # noqa: F811
    assert await server.busy(at(hour, minute)) == ""


async def test_headphones_may_be_spoken_to_in_class(server):  # noqa: F811
    async def call_on(node, tool, args):
        return ToolOutput([], "AirPods (headphones)" if tool == "get_audio_output" else "ok", False)

    server.herald.hub.call_on = call_on
    server.herald.busy = server.busy
    assert await server.herald.say("Hi", at(10, 45))


# ---- outcomes in the day note ----


async def test_outcomes_are_recorded_with_the_reason():
    records = []
    h, _told = make_herald(FakeHub(refuse="Didn't say it: the Mac is muted."))
    h.record = lambda *a: records.append(a)
    await h.deliver("W", "S", NOON)
    assert records == [("nudge", "W", "telegram (muted)")]
    h2, _ = make_herald(FakeHub(awake=False))
    h2.record = lambda *a: records.append(a)
    await h2.deliver("W", "S", NOON)
    assert records[-1][2] == "telegram"


# ---- supplements back off ----


def test_backoff_after_three_ignored_days_and_reset_when_logged_in_time():
    misses, day = {}, date(2026, 11, 10)
    for i in range(3):
        d = day + timedelta(days=i)
        assert not supplements.backed_off(misses, ["creatine"], d)
        supplements.update_misses(misses, ["creatine"], set(), d)
    fourth = day + timedelta(days=3)
    assert supplements.backed_off(misses, ["creatine"], fourth)
    assert not supplements.backed_off(misses, ["creatine"], fourth + timedelta(days=2))  # a gap breaks "in a row"
    supplements.update_misses(misses, ["creatine"], {"creatine"}, fourth)
    assert misses == {}


async def test_an_ignored_item_goes_to_telegram_only(server, tmp_path, monkeypatch):  # noqa: F811
    (tmp_path / "Nutrition").mkdir()
    (tmp_path / "Nutrition" / "Supplement Log.md").write_text("| date | time | item |\n")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[supplements]\nenabled = true\nlog = "Nutrition/Supplement Log.md"\nwindow_minutes = 120\n'
        'slots = [{ name = "morning", at = "10:00", say = "morning", items = ["creatine"] }]\n'
    )
    monkeypatch.setenv("SOMMUS_CONFIG", str(cfg))
    monkeypatch.setenv("SOMMUS_VAULT_PATH", str(tmp_path))
    for d in (6, 7, 8, 9):
        await server.supplement_nudges(at(10, 5, d))
    assert len(spoken(server)) == 3 and len(server.told) == 1  # three spoken, then the fourth day written only


# ---- the activity log knows where a turn came from ----


def test_the_note_says_where_a_turn_came_from_and_shows_empty_replies(tmp_path):
    store = Store(tmp_path / "s.db")
    turn = store.start_turn("what's left today", "iPhone")
    store.log_tool(turn, "supplements_left", {}, "read", "ran", False, "creatine")
    store.finish_turn(turn, "Creatine.", "ok", "fastpath", Usage())
    store.finish_turn(store.start_turn("hi"), "", "ok", "claude", Usage())
    day = store.day_turns(datetime.now().strftime("%Y-%m-%d"))
    note = activity.day_note("2026-11-16", day, [])
    assert "asked (iPhone):** what's left today" in note and "✓ `supplements_left`" in note
    assert "(nothing recorded)" in note


def test_an_old_database_gets_the_source_column(tmp_path):
    import sqlite3

    db = sqlite3.connect(tmp_path / "old.db")
    db.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, user_text TEXT NOT NULL,"
               " reply TEXT, status TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,"
               " cache_read_tokens INTEGER, cache_write_tokens INTEGER, cost_usd REAL)")  # fmt: skip
    db.commit()
    db.close()
    store = Store(tmp_path / "old.db")
    store.start_turn("x", "Telegram")
    assert store.day_turns(datetime.now().strftime("%Y-%m-%d"))[0]["source"] == "Telegram"
