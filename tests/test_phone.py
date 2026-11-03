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
    async with Client(phone.node()) as client:
        away = await client.call_tool("phone_alarm", {"time_24h": "13:05"})
        assert "set_reminder" in away.content[0].text and phone.action == {}
        phone.present = True
        said = await client.call_tool("phone_alarm", {"time_24h": "1:05 PM", "day": "2026-10-08"})
    assert said.content[0].text == "Setting an alarm on your phone for 1:05 PM."
    assert phone.action == {"action": "alarm", "time": "13:05", "at": "2026-10-08T13:05-04:00", "label": "Sommus"}


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
