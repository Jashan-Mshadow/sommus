"""Is Sommus OK: the free status answer, and the one-time alert when the Mac side stays offline."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_requests import ask, make

from sommus import status
from sommus.status import Presence

T = datetime(2026, 11, 26, 14, 0)


@pytest.mark.parametrize(
    "text",
    [
        "how's sommus doing",
        "Sommus status",
        "system status report",
        "is sommus running",
        "how is sommus going right now",
        "health check",
        "are you ok sommus",
    ],
)
def test_status_questions_are_claimed(text):
    assert status.is_question(text), text


@pytest.mark.parametrize(
    "text",
    [
        "how are you doing today my friend and what about the weather",  # long, other topics
        "what's the status of my ECE 150 project",  # about something else
        "turn the volume up",
        "is the lamp on",
        "how's the weather",
        "is everything ok",  # could be about anything
        "",
    ],
)
def test_other_questions_are_not(text):
    assert not status.is_question(text), text


def test_the_mac_alert_comes_once_after_the_wait_and_only_in_waking_hours():
    presence = Presence()
    assert presence.observe(True, T) is None  # first look: up
    assert presence.observe(False, T + timedelta(minutes=1)) is None  # just went down
    assert presence.observe(False, T + timedelta(minutes=20)) is None  # not long enough
    assert presence.observe(False, T + timedelta(minutes=35)) == "down"
    assert presence.observe(False, T + timedelta(minutes=60)) is None  # once per outage
    assert presence.observe(True, T + timedelta(minutes=90)) == "back"
    assert presence.observe(True, T + timedelta(minutes=95)) is None


def test_an_overnight_outage_waits_for_the_morning_and_a_short_one_is_never_reported():
    night = Presence()
    late = datetime(2026, 11, 26, 23, 40)
    night.observe(True, late)
    night.observe(False, late + timedelta(minutes=5))
    assert night.observe(False, late + timedelta(hours=2)) is None  # 1:45 AM: no 3 AM message about a sleeping Mac
    assert night.observe(False, datetime(2026, 11, 27, 10, 5)) == "down"  # still down once the day starts
    brief = Presence()
    brief.observe(True, T)
    brief.observe(False, T + timedelta(minutes=2))
    assert brief.observe(True, T + timedelta(minutes=10)) is None  # a blip: back before the wait, no alert, no "back"


def info(**overrides):
    base = dict(
        mac_nodes=["laptop"], reachable=lambda name: True, asleep=False, requests=4, fast=3, dollars=0.021,
        month_dollars=1.5, cap=10.0, data_dir=Path("/nonexistent"), now=T,
    )  # fmt: skip
    return {**base, **overrides}


def test_the_spoken_status_is_short_and_says_when_the_mac_is_down():
    assert (
        status.report(**info())
        == "All good. Up just started, awake. Mac side online. Today 4 requests, 3 of them free, 2 cents."
    )
    status.presence.up, status.presence.since = False, datetime(2026, 11, 26, 13, 10)
    try:
        said = status.report(**info(reachable=lambda name: False))
        assert said.startswith("The server is fine, but the Mac side is down.") and "offline since 1:10 PM" in said
    finally:
        status.presence.up, status.presence.since = None, None


def test_the_long_status_has_one_fact_per_line_and_the_nightly_job(tmp_path):
    (tmp_path / "training").mkdir()
    (tmp_path / "training" / "nightly.log").write_text(
        "=== run ===\nNight 8: 160 real examples, training: skipped\nsomething else\n"
    )
    lines = status.report(**info(data_dir=tmp_path, long=True)).splitlines()
    assert len(lines) == 5 and lines[3] == "This month: $1.50 of $10"
    assert lines[4] == "Last night's training job: Night 8: 160 real examples, training: skipped"


async def test_how_is_sommus_doing_is_answered_free_by_the_brain(tmp_path):
    brain, hub, _ = await make(tmp_path)  # no model replies queued: a model call would fail the test
    said = await ask(brain, "how's sommus doing")
    assert said.startswith("All good.") and "Today 0 requests" in said
    assert not brain.client.requests
    await hub.__aexit__(None, None, None)
