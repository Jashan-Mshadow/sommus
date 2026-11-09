"""2026-11-09 round: class nudges that repeated, the gym in the morning brief, quiet during classes, ticking off
to-dos, and the daily activity log."""

import json
import random
from datetime import date, datetime

import pytest
from test_announce import SAY_AS, ZONE, FakeHub, book, schedule, server  # noqa: F401

from sommus import activity, brief, herald
from sommus.brain import campus
from sommus.brain.store import Store
from sommus.nodes.vault.server import finish_item

# ---------------------------------------------------------------- the class nudge that repeated every tick


def test_old_keys_are_dropped_by_date_not_by_name():
    now = datetime(2026, 11, 9, 9, 50, tzinfo=ZONE)
    old = {("MATH 117 tutorial", f"2026-10-{d:02d}T14:30:00-04:00") for d in range(1, 29)}
    new = ("COMMST 192 lecture", "2026-11-09T10:00:00-05:00")
    kept = brief.recent_nudges(old | {new}, now)
    assert kept == [list(new)]  # the new one stays, however it sorts; October's are gone


async def test_a_class_is_nudged_once_even_with_a_full_history(server):  # noqa: F811
    server.state["brief"] = server.state["greeted"] = "2026-10-06"
    # 50 remembered nudges whose course sorts after "ECE 105": the old trim threw the new key away every tick
    server.state["nudged"] = [["MATH 117 tutorial", f"2026-10-0{d % 6}T14:{d:02d}:00-04:00"] for d in range(50)]
    for minute in (15, 16, 17, 18):
        await server.tick(datetime(2026, 10, 6, 8, minute, tzinfo=ZONE))
    assert len(server.hub_fake.calls) == 1


# ---------------------------------------------------------------- the morning brief


def test_the_brief_says_the_gym_day_but_not_the_workout(tmp_path):
    s = schedule(tmp_path)
    tue = datetime(2026, 10, 6, 7, 0, tzinfo=ZONE)
    gym = brief.gym_today({"days": {"Tue": "Day 1", "Wed": "Day 2"}}, tue.date())
    assert gym == "Day 1"
    assert brief.gym_today({"days": {"Tue": "Day 1"}}, date(2026, 10, 5)) == ""
    text = brief.morning(s, tue, gym=gym, events=["7 PM Hackathon info"], quip="Chest first, excuses never.")
    assert "Gym today: Day 1." in text and "Also today: 7 PM Hackathon info." in text
    assert text.endswith("Chest first, excuses never.")
    assert "Rest day, no gym." in brief.morning(s, tue)


def test_the_quip_fits_the_day():
    b = brief.Phrasebook(random.Random(1))
    assert brief.morning_quip("Day 1", 0, b) in brief.GYM_QUIPS
    assert brief.morning_quip("", 0, b) in brief.REST_QUIPS
    assert brief.morning_quip("", 3, b) in brief.BUSY_QUIPS


def test_the_spoken_greeting_mentions_the_gym(tmp_path):
    line = brief.spoken_morning(schedule(tmp_path), datetime(2026, 10, 6, 7, 40, tzinfo=ZONE), SAY_AS, book(),
                                gym="Day 1")  # fmt: skip
    assert "Gym today, Day 1." in line


# ---------------------------------------------------------------- quiet in class


async def test_nothing_is_said_aloud_while_busy_but_telegram_still_gets_it():
    told = []

    async def tell(text):
        told.append(text)

    async def busy(_now):
        return "ECE 105 lecture"

    hub = FakeHub()
    h = herald.Herald(hub, tell, {"quiet_hours": ["23:00", "07:30"]}, log=lambda _: None, busy=busy)
    assert not await h.deliver("Written.", "Spoken.", datetime(2026, 10, 6, 12, 0, tzinfo=ZONE))
    assert told == ["Written."] and hub.calls == []


async def test_the_server_knows_when_a_class_is_on(server, monkeypatch):  # noqa: F811
    assert await server.busy(datetime(2026, 10, 6, 8, 45, tzinfo=ZONE)) == "ECE 105 lecture"
    assert await server.busy(datetime(2026, 10, 6, 9, 30, tzinfo=ZONE)) == ""


# ---------------------------------------------------------------- "I'm done project 3"

TODO = """# TODO

## 📅 The next 7 days

## 🎓 School

- [ ] **ECE 150 P3:** finish the release tests *(added 2026-11-01)*
- [ ] **ECE 198:** update presentation Wed
- [ ] **ECE 198:** order sensor

## Done (recent)
| Date | What |
|---|---|
| 2026-11-04 | ECE 150 pointers |
"""


def test_finishing_ticks_the_item_and_adds_it_to_today_in_done():
    text, done, matches = finish_item(TODO, "done project 3 for 150", "public 8/8", "2026-11-09")
    assert "- [x] **ECE 150 P3:** finish the release tests *(added 2026-11-01)* *(done 2026-11-09: public 8/8)*" in text
    assert "| 2026-11-09 | ECE 150 P3: finish the release tests (public 8/8) |" in text
    assert text.index("2026-11-09 |") < text.index("2026-11-04 |")  # newest row first
    again, _, _ = finish_item(text, "presentation", "", "2026-11-09")
    assert (
        "| 2026-11-09 | ECE 150 P3: finish the release tests (public 8/8) · ECE 198: update presentation Wed |" in again
    )


def test_two_matches_change_nothing():
    text, _, matches = finish_item(TODO, "ECE 198", "", "2026-11-09")
    assert text == TODO and len(matches) == 2


def test_a_win_that_was_never_listed_is_still_recorded():
    text, done, matches = finish_item(TODO, "Hackathon signup", "", "2026-11-09")
    assert matches == [] and "| 2026-11-09 | Hackathon signup |" in text


# ---------------------------------------------------------------- the daily activity log


def test_the_activity_note_has_requests_tools_and_what_was_said_first(tmp_path):
    store = Store(tmp_path / "s.db")
    turn = store.start_turn("what's my next class")
    store.log_tool(turn, "next_class", {}, None, "ran", False, "Physics at 8:30")
    store.log_tool(turn, "send_email", {}, None, "needs_pin", True, "Locked")
    from sommus.brain.loop import Usage

    store.finish_turn(turn, "Physics at 8:30 in RCH 103.", "ok", "claude-sonnet", Usage())
    day = store.day_turns(datetime.now().strftime("%Y-%m-%d"))
    said = tmp_path / "said.jsonl"
    activity.record_said(said, "class nudge", "Physics in fifteen.", "spoken", datetime.now())
    note = activity.day_note(
        datetime.now().strftime("%Y-%m-%d"), day, activity.said_on(said, datetime.now().strftime("%Y-%m-%d"))
    )
    assert "1 requests" in note and "1 said first" in note and "1 tool calls not allowed or held" in note
    assert "asked:** what's my next class" in note and "✓ `next_class`" in note and "[needs_pin]" in note
    assert "said first** (class nudge, spoken): Physics in fifteen." in note
    path = activity.write_day(tmp_path / "vault", "2026-11-09", day, [])
    assert path.name == "2026-11-09.md" and path.parent.name == "Sommus Log"


async def test_nudges_land_in_the_said_log(server, tmp_path):  # noqa: F811
    server.herald.record = server.record_said
    server.reminders.add("Take creatine", "2026-10-06 21:30", datetime(2026, 10, 6, 9, 0))
    await server.fire_reminders(datetime(2026, 10, 6, 21, 30))
    lines = [json.loads(x) for x in server.said_path.read_text().splitlines()]
    assert lines[-1]["kind"] == "reminder" and lines[-1]["route"] == "spoken+telegram"


@pytest.fixture(autouse=True)
def _campus_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(campus, "cached", lambda _path: schedule(tmp_path))


def test_finishing_also_closes_the_one_matching_deadline():
    from sommus.nodes.vault.server import finish_deadline

    toml = (
        "deadlines = [\n"
        '  { what = "ECE 150 Project 3", due = 2026-11-10T22:00:00, where = "Marmoset" },\n'
        '  { what = "ECE 150 Project 4", due = 2026-11-24T22:00:00, where = "Marmoset" },\n]'
    )
    text, name = finish_deadline(toml, "done project 3 for 150")
    assert name == "ECE 150 Project 3"
    assert '{ what = "ECE 150 Project 3", due = 2026-11-10T22:00:00, where = "Marmoset", done = true },' in text
    assert finish_deadline(toml, "ECE 150") == (toml, "")  # two match: close neither


def test_a_done_deadline_is_not_due_soon(tmp_path):
    path = tmp_path / "schedule.toml"
    path.write_text(
        'timezone = "America/Toronto"\nterm_start = 2026-09-08\nterm_end = 2026-12-08\nweekly = []\n'
        'deadlines = [\n  { what = "P3", due = 2026-11-10T22:00:00, done = true },\n'
        '  { what = "P4", due = 2026-11-11T22:00:00 },\n]\n'
    )
    s = campus.Schedule.load(path)
    assert [d.what for d in s.due_within(datetime(2026, 11, 9, 9, 0, tzinfo=ZONE), 3)] == ["P4"]
