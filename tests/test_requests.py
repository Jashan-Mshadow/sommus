"""A request said twice is answered once; to-do answers come from the clock; the loop's room-chatter rules."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fakes import FakeModel, build_node, config, text_reply, tool_call
from mcp import Client

from sommus.brain import campus, loop
from sommus.brain.loop import Brain, TextDelta
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store

ZONE = ZoneInfo("America/Toronto")


async def ask(brain, text):
    async def allow(name, args):
        return True

    return "".join([e.text async for e in brain.handle(text, allow) if isinstance(e, TextDelta)])


async def make(tmp_path, *responses):
    node, calls = build_node()
    hub = NodeHub((), Policy())
    await hub.__aenter__()
    await hub.add("test", Client(node))
    brain = Brain(config(tmp_path, ask=False), hub, Store(tmp_path / "t.db"), client=FakeModel(*responses))
    return brain, hub, calls


# ------------------------------------------------------------------ the same request twice


async def test_the_same_question_twice_is_answered_once(tmp_path):
    brain, hub, calls = await make(tmp_path, tool_call("peek", {}), text_reply("All quiet."))
    first = await ask(brain, "Is anything happening on the box?")
    second = await ask(brain, "is anything   happening on the box")  # same words, other spacing and case
    assert first == second == "All quiet."
    assert calls == ["peek"] and len(brain.client.requests) == 2  # the second one made no model call at all
    rows = brain.store._db.execute("SELECT user_text, model FROM turns ORDER BY id").fetchall()
    assert [m for _, m in rows][-1] == "repeat" and len(rows) == 2  # still logged
    await hub.__aexit__(None, None, None)


async def test_a_request_that_changed_something_is_never_repeated_from_memory(tmp_path):
    brain, hub, calls = await make(
        tmp_path, tool_call("wipe", {"target": "a"}), text_reply("Wiped."), tool_call("wipe", {"target": "a"}),
        text_reply("Wiped again."),
    )  # fmt: skip
    assert await ask(brain, "wipe a") == "Wiped."
    assert await ask(brain, "wipe a") == "Wiped again."
    assert calls == ["wipe a", "wipe a"]
    await hub.__aexit__(None, None, None)


async def test_a_repeat_on_another_channel_or_after_30_seconds_runs_again(tmp_path, monkeypatch):
    brain, hub, _ = await make(tmp_path, text_reply("One."), text_reply("Two."), text_reply("Three."))
    now = [1000.0]
    monkeypatch.setattr(loop.time, "monotonic", lambda: now[0])
    assert await ask(brain, "tell me something") == "One."
    brain.channel = "iPhone"
    assert await ask(brain, "tell me something") == "Two."  # other channel: not the same request
    now[0] += loop.REPEAT_SECONDS + 1
    assert await ask(brain, "tell me something") == "Three."  # too long ago
    await hub.__aexit__(None, None, None)


async def test_a_fast_path_turn_in_between_forgets_the_old_answer(tmp_path):
    brain, hub, _ = await make(tmp_path, text_reply("It is 40."), text_reply("It is 60."))
    assert await ask(brain, "how are things looking") == "It is 40."
    await ask(brain, "what time is it")  # answered by plain code: something may have changed around it
    assert await ask(brain, "how are things looking") == "It is 60."
    await hub.__aexit__(None, None, None)


# ------------------------------------------------------------------ to-do answers from the clock


def _vault(tmp_path, monkeypatch, todo: str):
    schedule = tmp_path / "schedule.toml"
    schedule.write_text("""
term_start = 2026-09-08
term_end = 2026-12-08
weekly = [
  { course = "ECE 150", kind = "LEC", day = "Tue", start = 08:30:00, end = 09:20:00, room = "E7 5353" },
  { course = "MATH 117", kind = "TUT", day = "Tue", start = 14:30:00, end = 16:20:00, room = "DWE 3517" },
  { course = "ECE 105", kind = "LEC", day = "Wed", start = 10:30:00, end = 11:20:00, room = "RCH 101" },
]
deadlines = [{ what = "Lab report", due = 2026-11-17T22:00:00 }, { what = "Quiz 4", due = 2026-11-18T23:59:00 }]
""")
    note = tmp_path / "TODO.md"
    note.write_text(todo)
    monkeypatch.setattr(campus, "schedule_path", lambda: schedule)
    monkeypatch.setattr(campus, "todo_path", lambda: note)


TODO = """---
type: todo
updated: 2026-11-17
---
# TODO
## Today
- [ ] 1. Finish the lab report
- [x] 2. Email the TA
- [ ] 3. Gym
## Done
- [x] Old thing
"""


def test_the_todo_answer_has_what_is_ahead_today_and_tomorrow_and_nothing_checked_off(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch, TODO)
    brain = Brain.__new__(Brain)  # _todo only needs the clock and the files
    now = datetime(2026, 11, 17, 20, 0, tzinfo=ZONE)  # Tuesday 8 PM: both classes are over
    said = brain._todo(["whats", "on", "my", "to", "do", "list", "rest", "of", "today", "and", "tomorrow"], now)
    assert "No classes left today." in said and "MATH 117" not in said and "ECE 150" not in said
    assert "Tomorrow: 10:30 AM ECE 105 lecture in RCH 101." in said
    assert "Due today: Lab report at 10 PM" in said and "due tomorrow: Quiz 4" in said
    assert "Finish the lab report" in said and "Gym" in said
    assert "Email the TA" not in said and "Old thing" not in said


def test_an_old_todo_still_carries_the_staleness_warning(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch, TODO.replace("updated: 2026-11-17", "updated: 2026-11-15"))
    said = Brain.__new__(Brain)._todo(["todo", "today"], datetime(2026, 11, 17, 12, 0, tzinfo=ZONE))
    assert said.startswith("Heads up, the list was last updated Sunday")
    assert "MATH 117 tutorial" in said  # still ahead at noon


# ------------------------------------------------------------------ overheard speech


def test_a_short_fragment_is_ignored_but_a_command_or_the_name_is_not():
    assert loop.overheard("the derivative of", False)
    assert loop.overheard("mm hmm okay", False)
    assert loop.overheard("what's the weather", False) is None
    assert loop.overheard("open spotify", False) is None
    assert loop.overheard("sommus", False) is None
    assert loop.overheard("yes", False, recent_reply=True) is None  # an answer to what Sommus just said
    assert (
        loop.overheard("so the integral of x squared from zero to three is nine", False) is None
    )  # long: the model decides


def test_nothing_is_acted_on_during_a_class_unless_he_says_the_name():
    assert loop.overheard("what's the volume", True) == "in a class"
    assert loop.overheard("set the volume to 30", True) == "in a class"
    assert loop.overheard("hey sommus what's next", True) is None


def test_volume_and_brightness_need_a_number_or_their_own_word():
    assert loop.level_unasked("set_volume", "okay that is interesting")
    assert not loop.level_unasked("set_volume", "set it to 30")
    assert not loop.level_unasked("set_volume", "a bit louder")
    assert not loop.level_unasked("set_volume", "turn the volume down")
    assert loop.level_unasked("set_brightness", "that's loud")
    assert not loop.level_unasked("set_brightness", "dim the screen")
    assert not loop.level_unasked("get_volume", "anything")  # only the setters
