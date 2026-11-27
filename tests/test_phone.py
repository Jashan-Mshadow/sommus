"""The iPhone's own actions: what phone_alarm queues, and what /ask hands the Shortcut.

Regression for the alarm set for 1:05 PM on Oct 8 that never rang: the Shortcut had no branch for "alarm", the
tool said "Alarm set" anyway, and the time and day weren't checked.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeModel, config, text_reply, tool_call
from mcp import Client

from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store
from sommus.server import Phone, Server, alarm_time, pin_local_time

ZONE = ZoneInfo("America/Toronto")
MORNING = datetime(2026, 10, 8, 9, 0, tzinfo=ZONE)  # Thursday Oct 8, 9:00 in Waterloo


@pytest.mark.parametrize(
    "said, expected",
    [
        ("13:05", datetime(2026, 10, 8, 13, 5, tzinfo=ZONE)),
        ("1:05 PM", datetime(2026, 10, 8, 13, 5, tzinfo=ZONE)),
        ("1:05pm", datetime(2026, 10, 8, 13, 5, tzinfo=ZONE)),
        ("1:05 p.m.", datetime(2026, 10, 8, 13, 5, tzinfo=ZONE)),
        ("12 am", datetime(2026, 10, 9, 0, 0, tzinfo=ZONE)),
        ("12:30 pm", datetime(2026, 10, 8, 12, 30, tzinfo=ZONE)),
        ("0730", datetime(2026, 10, 9, 7, 30, tzinfo=ZONE)),  # already past today: tomorrow
        ("7", datetime(2026, 10, 9, 7, 0, tzinfo=ZONE)),
    ],
)
def test_alarm_times_are_read_in_jashans_zone_and_ring_at_the_next_one(said, expected):
    assert alarm_time(said, "", MORNING) == expected


def test_a_named_day_is_kept_and_one_an_iphone_alarm_cant_reach_says_use_a_reminder():
    assert alarm_time("1:05 PM", "2026-10-08", MORNING) == datetime(2026, 10, 8, 13, 5, tzinfo=ZONE)
    with pytest.raises(ValueError, match="set_reminder for 2026-10-10 13:05"):
        alarm_time("13:05", "2026-10-10", MORNING)
    with pytest.raises(ValueError, match="already passed"):
        alarm_time("8:00", "2026-10-08", MORNING)


@pytest.mark.parametrize("said", ["25:00", "13:05 pm", "noonish", "", "7:75"])
def test_times_that_arent_times_are_refused(said):
    with pytest.raises(ValueError):
        alarm_time(said, "", MORNING)


async def test_the_alarm_is_queued_with_a_24_hour_time_and_said_honestly():
    phone = Phone(ZONE, clock=lambda: MORNING)
    phone.confirmed("alarm")  # the Shortcut has made one before
    async with Client(phone.node()) as client:
        away = await client.call_tool("phone_alarm", {"time_24h": "13:05"})
        assert "set_reminder" in away.content[0].text and phone.action == {}
        phone.present = True
        said = await client.call_tool("phone_alarm", {"time_24h": "1:05 PM", "day": "2026-10-08"})
    assert said.content[0].text == "Setting an alarm on your phone for 1:05 PM."
    assert phone.action == {"action": "alarm", "time": "13:05", "at": "2026-10-08T13:05-04:00", "label": "Sommus"}


async def test_away_from_the_iphone_it_says_no_alarm_was_set_and_offers_a_wake_up():
    """2026-11-26: from the iPhone, phone_alarm said "Set it for 7:50 PM" and Clock showed nothing. From Telegram
    the phone can't be reached at all, so the reply must not read like success."""
    phone = Phone(ZONE, clock=lambda: MORNING)
    async with Client(phone.node()) as client:
        away = (await client.call_tool("phone_alarm", {"time_24h": "13:05"})).content[0].text
    assert away.startswith("No phone alarm was set") and "schedule_wakeup" in away and phone.action == {}


async def test_until_the_shortcut_confirms_an_alarm_the_reply_never_says_it_is_set(tmp_path):
    clock = [MORNING]
    phone = Phone(ZONE, clock=lambda: clock[0], state=tmp_path / "phone.json")
    phone.present = True
    async with Client(phone.node()) as client:
        first = (await client.call_tool("phone_alarm", {"time_24h": "13:05"})).content[0].text
        assert phone.action["action"] == "alarm"  # still handed over: the Shortcut may have the step
        assert "never confirmed" in first and "Do not say the alarm is set" in first
        assert "Setting an alarm" not in first
        clock[0] = MORNING.replace(minute=1)
        phone.confirmed("alarm")  # /phone/done from the Shortcut's alarm branch
        phone.action = {}
        clock[0] = MORNING.replace(minute=2)
        second = (await client.call_tool("phone_alarm", {"time_24h": "14:00"})).content[0].text
        assert second == "Setting an alarm on your phone for 2:00 PM."
        phone.action = {}
        clock[0] = MORNING.replace(minute=3)  # that one was never confirmed: the Shortcut lost its step again
        third = (await client.call_tool("phone_alarm", {"time_24h": "15:00"})).content[0].text
        assert "never confirmed" in third
    assert Phone(ZONE, state=tmp_path / "phone.json").alarms["confirmed"] == MORNING.replace(minute=1).isoformat()


def test_the_shortcut_confirms_through_phone_done_from_his_devices_only(tmp_path):
    from starlette.testclient import TestClient

    phone = Phone(ZONE, clock=lambda: MORNING)
    server = Server(config(tmp_path), NodeHub((), Policy()), Store(tmp_path / "t.db"), phone, log=lambda _l: None)
    server.tailnet.is_mine = lambda address: address == "testclient"
    with TestClient(server.app()) as http:
        assert http.post("/phone/done", json={"action": "alarm"}).json() == {"ok": True}
    assert phone.alarms_work()
    server.tailnet.is_mine = lambda address: False
    with TestClient(server.app()) as http:
        assert http.post("/phone/done", json={"action": "alarm"}).status_code == 403


def test_a_backup_alarm_for_a_wake_up_goes_only_to_a_phone_that_can_make_one():
    phone = Phone(ZONE, clock=lambda: MORNING)
    at = datetime(2026, 10, 9, 7, 50)
    assert phone.backup_alarm(at) == ""  # not from the iPhone
    phone.present = True
    assert phone.backup_alarm(at) == "" and phone.action == {}  # never confirmed: nothing claimed, nothing queued
    phone.confirmed("alarm")
    assert phone.backup_alarm(at) == "Your phone's Clock gets an alarm for 7:50 AM tomorrow too."
    assert phone.action["time"] == "07:50" and phone.action["label"] == "Wake up"


async def test_a_bad_alarm_time_queues_nothing():
    phone = Phone(ZONE, clock=lambda: MORNING)
    phone.present = True
    async with Client(phone.node()) as client:
        said = await client.call_tool("phone_alarm", {"time_24h": "half past lunch"})
    assert "Couldn't read" in said.content[0].text and phone.action == {}


async def test_ask_hands_the_shortcut_the_alarm_and_clears_it_for_the_next_request(tmp_path):
    phone = Phone(ZONE, clock=lambda: MORNING)
    async with NodeHub((), Policy()) as hub:
        await hub.add("phone", Client(phone.node()))
        server = Server(config(tmp_path), hub, Store(tmp_path / "t.db"), phone, log=lambda _line: None)
        server.voice_brain.client = FakeModel(
            tool_call("phone_alarm", {"time_24h": "13:05", "label": "Lab"}),
            text_reply("Alarm for 1:05."),
            text_reply("Sure."),
        )
        first = await server.ask("set an alarm for 1:05 pm")
        second = await server.ask("thanks")
    assert first["action"] == "alarm" and first["time"] == "13:05" and first["label"] == "Lab"
    assert first["reply"] == "Alarm for 1:05."
    assert second["action"] == "" and "time" not in second  # drained: one request, one action
    assert phone.present is False


def test_the_servers_clock_is_pinned_to_jashans_zone(monkeypatch):
    import time

    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        assert pin_local_time("America/Toronto") is True
        assert datetime(2026, 10, 8, 12, 0).astimezone().utcoffset().total_seconds() == -4 * 3600
        assert pin_local_time("America/Toronto") is False
    finally:
        monkeypatch.undo()
        time.tzset()


async def test_music_and_focus_are_queued_for_the_shortcut_and_refused_away_from_the_phone():
    phone = Phone(ZONE, clock=lambda: MORNING)
    async with Client(phone.node()) as client:
        away = await client.call_tool("phone_music", {"what": "Lofi Study"})
        assert "iPhone" in away.content[0].text and phone.action == {}
        phone.present = True
        await client.call_tool("phone_music", {"what": " Lofi Study "})
        assert phone.action == {"action": "music", "control": "play", "what": "Lofi Study"}
        phone.action = {}
        bad = await client.call_tool("phone_music", {"control": "explode"})
        assert "must be" in bad.content[0].text and phone.action == {}
        await client.call_tool("phone_focus", {"mode": "Sleep", "on": False})
        assert phone.action == {"action": "focus_off", "mode": "Sleep"}


def test_a_clashing_note_is_merged_not_retried_forever(tmp_path):
    import subprocess

    from sommus.server import sync_vault

    def git(cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)

    origin, mac, srv = tmp_path / "origin.git", tmp_path / "mac", tmp_path / "srv"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(mac)], check=True, capture_output=True)
    for repo in (mac,):
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
    (mac / ".gitattributes").write_text("*.md merge=union\n")
    (mac / "note.md").write_text("a\n")
    git(mac, "add", "-A")
    git(mac, "commit", "-qm", "start")
    git(mac, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(srv)], check=True, capture_output=True)
    git(srv, "config", "user.email", "t@t")
    git(srv, "config", "user.name", "t")
    (mac / "note.md").write_text("a\nfrom the mac\n")
    git(mac, "commit", "-qam", "mac edit")
    git(mac, "push", "-q", "origin", "main")
    (srv / "note.md").write_text("a\nfrom the server\n")  # the same spot: a plain rebase would stop here
    lines: list[str] = []
    sync_vault(srv, lines.append)
    text = (srv / "note.md").read_text()
    assert "from the mac" in text and "from the server" in text and "<<<<" not in text
    assert lines == []
