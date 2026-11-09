"""Sommus speaking first: the Mac's announce tool, the spoken phrasings, and the voice-or-Telegram route.
Nothing here plays sound: the voice is a fake that records what it was given."""

import asyncio
import random
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from mcp import Client

from sommus import brief, herald
from sommus.brain import campus
from sommus.brain.nodes import ToolOutput
from sommus.nodes.laptop import macos, speak

ZONE = ZoneInfo("America/Toronto")
SAY_AS = {"ECE 105": "physics", "MATH 117": "calculus", "ECE 150": "programming"}
SCHEDULE = """
timezone = "America/Toronto"
term_start = 2026-09-08
term_end = 2026-12-08
weekly = [
  { course = "ECE 105", kind = "LEC", day = "Tue", start = 08:30:00, end = 09:20:00, room = "RCH 103" },
  { course = "MATH 117", kind = "TUT", day = "Tue", start = 10:30:00, end = 11:20:00, room = "TBA" },
]
deadlines = [
  { what = "ECE 150 assignment 3", due = 2026-10-07T23:59:00 },
  { what = "MATH 117 quiz", due = 2026-10-07T12:00:00 },
]
"""


def schedule(tmp_path):
    path = tmp_path / "schedule.toml"
    path.write_text(SCHEDULE)
    return campus.Schedule.load(path)


def book(seed=0):
    return brief.Phrasebook(random.Random(seed))


# ---------------------------------------------------------------- what it says


def test_numbers_are_said_in_words():
    assert [brief.words(n) for n in (1, 15, 20, 42, 59)] == ["one", "fifteen", "twenty", "forty-two", "fifty-nine"]


def test_a_class_nudge_names_the_subject_and_room_not_the_code(tmp_path):
    s = schedule(tmp_path)
    now = datetime(2026, 10, 6, 8, 15, tzinfo=ZONE)
    [session] = brief.classes_starting(s, now, 15, set())
    lines = {brief.spoken_class_nudge(session, now, SAY_AS, book(seed)) for seed in range(20)}
    assert "Physics in fifteen, room RCH 103." in lines
    assert len(lines) > 2  # it has more than one way to say it
    for line in lines:
        assert "ECE" not in line and "lecture" not in line and "fifteen" in line.lower() and "RCH 103." in line
    assert brief.class_nudge(session, now) == "ECE 105 lecture in 15 minutes, in RCH 103."  # the screen's one


def test_a_class_without_a_room_leaves_the_room_out(tmp_path):
    s = schedule(tmp_path)
    now = datetime(2026, 10, 6, 10, 16, tzinfo=ZONE)
    [session] = brief.classes_starting(s, now, 15, set())
    for seed in range(10):
        line = brief.spoken_class_nudge(session, now, SAY_AS, book(seed))
        assert "calculus tutorial" in line.lower() and "TBA" not in line and "room" not in line


def test_the_phrasebook_never_says_the_same_thing_twice_in_a_row():
    phrases = book()
    picks = [phrases.pick("class", brief.CLASS_SOON) for _ in range(50)]
    assert all(a != b for a, b in zip(picks, picks[1:], strict=False))
    assert phrases.pick("solo", ["only one"]) == phrases.pick("solo", ["only one"]) == "only one"


def test_reminders_and_timers_sound_like_speech():
    line = brief.spoken_reminder("Take creatine", book())
    assert line.endswith("take creatine.") and line != "take creatine."
    assert brief.spoken_reminder("ECE 190 form", book()).endswith("ECE 190 form.")  # codes keep their case
    assert "ten minute" in brief.spoken_reminder("Timer (10 min)", book())
    late = brief.spoken_reminder("Call Mom", book(), datetime(2026, 10, 6, 21, 30))
    assert late.endswith("That was meant for 9:30 PM.")


def test_due_tomorrow_and_the_morning_greeting(tmp_path):
    s = schedule(tmp_path)
    due = brief.spoken_due_tomorrow(s, datetime(2026, 10, 6, 20, 0, tzinfo=ZONE), SAY_AS, book())
    assert "calculus quiz and programming assignment 3" in due and ("are due" in due or "you've got" in due)
    assert brief.spoken_due_tomorrow(s, datetime(2026, 10, 8, 20, 0, tzinfo=ZONE), SAY_AS, book()) == ""
    hello = brief.spoken_morning(s, datetime(2026, 10, 6, 7, 40, tzinfo=ZONE), SAY_AS, book(), "Jashan", "4 degrees.")
    assert "Jashan" in hello and "First up is physics at 8:30 AM, then one more." in hello
    assert hello.endswith("4 degrees.")
    assert "No classes today." in brief.spoken_morning(s, datetime(2026, 10, 7, 8, 0, tzinfo=ZONE), SAY_AS, book())


# ---------------------------------------------------------------- the Mac's announce tool


class FakeVoice:
    def __init__(self, fail=False):
        self.fail, self.said = fail, []

    def synthesize(self, sentence):
        if self.fail:
            raise RuntimeError("Kokoro didn't load")
        return sentence

    def play(self, audio, stop):
        self.said.append(audio)

    def rest(self, stop):
        pass


@pytest.fixture
def announcer(tmp_path, monkeypatch):
    a = speak.Announcer(tmp_path / "quiet")
    a._settings = {"say_as": SAY_AS}
    spoken = []
    monkeypatch.setattr(a, "_speak", spoken.append)  # no thread, no sound
    monkeypatch.setattr(speak, "announcer", a)
    monkeypatch.setattr(macos, "volume", lambda: macos.VolumeState(50, False))
    a.spoken = spoken
    return a


async def test_announce_speaks_with_subjects_not_codes(announcer):
    from sommus.nodes.laptop.server import server

    async with Client(server) as client:
        result = await client.call_tool("announce", {"text": "ECE 105 LEC in 15 minutes."})
    assert not result.is_error
    assert announcer.spoken == ["physics lecture in 15 minutes."]


async def test_announce_stays_silent_when_muted_or_quiet(announcer, monkeypatch):
    from sommus.nodes.laptop.server import server

    async with Client(server) as client:
        monkeypatch.setattr(macos, "volume", lambda: macos.VolumeState(50, True))
        muted = await client.call_tool("announce", {"text": "Hello."})
        monkeypatch.setattr(macos, "volume", lambda: macos.VolumeState(0, False))
        zero = await client.call_tool("announce", {"text": "Hello."})
        monkeypatch.setattr(macos, "volume", lambda: macos.VolumeState(50, False))
        quiet = await client.call_tool("set_quiet", {"on": True})
        hushed = await client.call_tool("announce", {"text": "Hello."})
        await client.call_tool("set_quiet", {"on": False})
        back = await client.call_tool("announce", {"text": "Hello."})
    assert muted.is_error and muted.content[0].text.endswith("Didn't say it: the Mac is muted.")
    assert zero.is_error and "volume is at zero" in zero.content[0].text
    assert "Quiet mode on" in quiet.content[0].text
    assert hushed.is_error and "quiet mode" in hushed.content[0].text
    assert not back.is_error and announcer.spoken == ["Hello."]


def test_kokoro_failing_falls_back_to_the_built_in_voice(tmp_path, monkeypatch):
    a = speak.Announcer(tmp_path / "quiet")
    a._settings = {}
    a.voice, say = FakeVoice(fail=True), FakeVoice()
    monkeypatch.setattr(a, "_say", lambda: say)
    a._speak("Physics in fifteen.")
    assert say.said == ["Physics in fifteen."] and a.voice is say


# ---------------------------------------------------------------- voice or Telegram


def test_quiet_hours_wrap_past_midnight():
    start, end = time(23, 0), time(7, 30)
    assert herald.in_quiet_hours(time(23, 30), start, end) and herald.in_quiet_hours(time(3, 0), start, end)
    assert not herald.in_quiet_hours(time(7, 30), start, end) and not herald.in_quiet_hours(time(12, 0), start, end)
    assert herald.in_quiet_hours(time(13, 0), time(12, 0), time(14, 0))
    assert not herald.in_quiet_hours(time(13, 0), time(9, 0), time(9, 0))


class FakeHub:
    def __init__(self, awake=True, refuse="", hang=False):
        self.awake, self.refuse, self.hang, self.calls = awake, refuse, hang, []

    def reachable(self, node):
        return self.awake

    async def call_on(self, node, tool, args):
        self.calls.append((node, tool, args))
        if self.hang:
            await asyncio.sleep(5)
        if self.refuse:
            return ToolOutput.failure(self.refuse)
        return ToolOutput([], f"Said: {args.get('text', '')}", False)


def make_herald(hub, settings=None):
    told = []

    async def tell(text):
        told.append(text)

    h = herald.Herald(hub, tell, settings or {"quiet_hours": ["23:00", "07:30"]}, log=lambda _: None)
    return h, told


NOON = datetime(2026, 10, 6, 12, 0)


async def test_an_awake_mac_hears_it_and_telegram_does_not():
    hub = FakeHub()
    h, told = make_herald(hub)
    assert await h.deliver("ECE 105 lecture in 15 minutes.", "Physics in fifteen.", NOON)
    assert hub.calls == [("laptop", "announce", {"text": "Physics in fifteen."})] and told == []


@pytest.mark.parametrize(
    "hub, now, settings",
    [
        (FakeHub(awake=False), NOON, None),  # the Mac is asleep
        (FakeHub(refuse="Didn't say it: the Mac is muted."), NOON, None),
        (FakeHub(), datetime(2026, 10, 6, 23, 30), None),  # quiet hours
        (FakeHub(), NOON, {"speak": False}),
    ],
)
async def test_otherwise_it_goes_to_telegram(hub, now, settings):
    h, told = make_herald(hub, settings)
    assert not await h.deliver("ECE 105 lecture in 15 minutes.", "Physics in fifteen.", now)
    assert told == ["ECE 105 lecture in 15 minutes."]
    assert len(hub.calls) == (1 if hub.refuse else 0)  # asleep, quiet hours or turned off: the Mac isn't asked


async def test_a_mac_that_hangs_falls_back_to_telegram(monkeypatch):
    monkeypatch.setattr(herald, "SAY_TIMEOUT", 0.05)
    h, told = make_herald(FakeHub(hang=True))
    assert not await h.deliver("Written.", "Spoken.", NOON)
    assert told == ["Written."]


async def test_reminders_reach_telegram_even_when_spoken():
    h, told = make_herald(FakeHub())
    assert await h.deliver("⏰ Take creatine", "Reminder: take creatine.", NOON, telegram_too=True)
    assert told == ["⏰ Take creatine"]


# ---------------------------------------------------------------- the server's scheduler


@pytest.fixture
def server(tmp_path, monkeypatch):
    import fakes

    from sommus.brain.nodes import NodeHub
    from sommus.brain.permissions import Policy
    from sommus.brain.store import Store
    from sommus.reminders import Reminders
    from sommus.server import Phone, Server

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")  # the brain is built but never asked anything
    cfg = fakes.config(tmp_path)
    s = Server(cfg, NodeHub((), Policy()), Store(tmp_path / "s.db"), Phone(), log=lambda _: None,
               reminders=Reminders(tmp_path / "reminders.json"))  # fmt: skip
    hub, told = FakeHub(), []

    async def tell(text):
        told.append(text)

    s.herald = herald.Herald(hub, tell, {"quiet_hours": ["23:00", "07:30"]}, log=lambda _: None)
    s.say_as, s.phrases = SAY_AS, book()
    monkeypatch.setattr(campus, "cached", lambda _path: schedule(tmp_path))
    monkeypatch.setattr(s, "_weather", lambda: "")
    s.hub_fake, s.told = hub, told
    return s


async def test_the_class_nudge_is_spoken_on_the_awake_mac(server, monkeypatch):
    monkeypatch.setattr(server, "tell", server.herald.tell)
    server.state["brief"] = server.state["greeted"] = "2026-10-06"
    await server.tick(datetime(2026, 10, 6, 8, 15, tzinfo=ZONE))
    spoken = [args["text"] for _node, _tool, args in server.hub_fake.calls]
    assert len(spoken) == 1 and spoken[0].lower().startswith(("physics", "heads up, physics", "fifteen minutes"))
    assert server.told == []
    await server.tick(datetime(2026, 10, 6, 8, 16, tzinfo=ZONE))
    assert len(server.hub_fake.calls) == 1  # once


async def test_a_fired_reminder_is_spoken_and_sent(server):
    server.reminders.add("Take creatine", "2026-10-06 21:30", datetime(2026, 10, 6, 9, 0))
    await server.fire_reminders(datetime(2026, 10, 6, 21, 30))
    assert server.told == ["⏰ Take creatine"]
    assert server.hub_fake.calls[0][2]["text"].endswith("take creatine.")


async def test_good_morning_waits_for_the_end_of_quiet_hours_and_comes_once(server):
    sched, morning = campus.cached(None), datetime(2026, 10, 6, 7, 0, tzinfo=ZONE)
    await server.greet(sched, morning.replace(minute=10), morning)
    assert server.hub_fake.calls == []  # 7:10 is still quiet hours
    await server.greet(sched, morning.replace(minute=45), morning)
    await server.greet(sched, morning.replace(minute=50), morning)
    assert len(server.hub_fake.calls) == 1 and "First up is physics" in server.hub_fake.calls[0][2]["text"]
