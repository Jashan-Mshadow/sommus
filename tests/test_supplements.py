"""Supplement nudges: read the vault's log, speak up only about what's missing, once per slot per day."""

import random
from datetime import date, datetime

import pytest
from test_announce import ZONE, server  # noqa: F401  (the scheduler fixture lives in test_announce)

from sommus import brief, supplements

DAY = date(2026, 11, 9)
LOG = """---
name: Supplement Log
---
# Supplement log

| date | time | item | note |
|---|---|---|---|
| 2026-11-08 | 18:10 | fish oil | yesterday |
| 2026-11-09 | 09:30 | Multivitamin | |
| 2026-11-09 | 09:30 | hair gummies | |
| 2026-11-09 | 18:05 | omega-3 | with dinner |
"""
SLOTS = [
    {"name": "morning", "at": "10:00", "say": "morning", "items": ["multivitamin", "hair gummies"]},
    {"name": "dinner", "at": "18:00", "say": "around dinner", "items": ["fish oil", "k2+d3"]},
]


def test_the_log_is_read_for_one_day_and_names_are_normalised():
    assert supplements.taken_on(LOG, DAY) == {"multivitamin", "hair gummies", "fish oil"}
    assert supplements.taken_on(LOG, date(2026, 11, 8)) == {"fish oil"}
    assert supplements.taken_on("", DAY) == set()
    assert supplements.canon("D3") == supplements.canon("K2+D3") == "k2+d3"


def test_only_what_is_missing_is_named():
    taken = supplements.taken_on(LOG, DAY)
    assert supplements.missing(["fish oil", "k2+d3"], taken) == ["k2+d3"]
    assert supplements.missing(["multivitamin", "hair gummies"], taken) == []


def test_a_slot_is_due_inside_its_window_and_once():
    at = lambda h, m: datetime(2026, 11, 9, h, m, tzinfo=ZONE)  # noqa: E731
    assert supplements.slots_due(SLOTS, at(17, 59), 120, set()) == []  # dinner not yet, morning long past
    assert [s["name"] for s in supplements.slots_due(SLOTS, at(18, 1), 120, set())] == ["dinner"]
    assert supplements.slots_due(SLOTS, at(20, 30), 120, set()) == []  # too late to be useful
    done = {supplements.key_for(SLOTS[1], at(18, 1))}
    assert supplements.slots_due(SLOTS, at(18, 5), 120, done) == []


def test_the_nudge_names_only_the_missing_items_in_both_voices():
    now = datetime(2026, 11, 9, 18, 1, tzinfo=ZONE)
    assert supplements.written(SLOTS[1], ["k2+d3"], now) == "Supplements, around dinner: K2+D3 not logged yet."
    lines = {supplements.spoken(SLOTS[1], ["fish oil", "k2+d3"], brief.Phrasebook(random.Random(i))) for i in range(20)}
    assert len(lines) > 1
    assert all("fish oil and K2 and D3" in line.lower() or "fish oil and k2 and d3" in line.lower() for line in lines)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "Nutrition").mkdir()
    (tmp_path / "Nutrition" / "Supplement Log.md").write_text(LOG)
    config = tmp_path / "config.toml"
    config.write_text(
        '[supplements]\nenabled = true\nlog = "Nutrition/Supplement Log.md"\nwindow_minutes = 120\nslots = [\n'
        '  { name = "morning", at = "10:00", say = "morning", items = ["multivitamin", "hair gummies"] },\n'
        '  { name = "dinner", at = "18:00", say = "around dinner", items = ["fish oil", "k2+d3"] },\n]\n'
    )
    monkeypatch.setenv("SOMMUS_CONFIG", str(config))
    monkeypatch.setenv("SOMMUS_VAULT_PATH", str(tmp_path))
    return tmp_path


async def test_the_server_nudges_about_what_is_missing_once(server, vault):  # noqa: F811
    await server.supplement_nudges(datetime(2026, 11, 9, 18, 2, tzinfo=ZONE))
    spoken = [args["text"] for _node, _tool, args in server.hub_fake.calls]
    assert len(spoken) == 1 and "k2" in spoken[0].lower() and "fish oil" not in spoken[0].lower()
    await server.supplement_nudges(datetime(2026, 11, 9, 18, 30, tzinfo=ZONE))
    assert len(server.hub_fake.calls) == 1  # once per slot per day


async def test_nothing_is_said_when_everything_in_the_slot_is_logged(server, vault):  # noqa: F811
    await server.supplement_nudges(datetime(2026, 11, 9, 10, 5, tzinfo=ZONE))
    assert server.hub_fake.calls == [] and server.told == []
    await server.supplement_nudges(datetime(2026, 11, 9, 10, 20, tzinfo=ZONE))  # marked done, still quiet
    assert server.hub_fake.calls == []


async def test_a_missing_log_file_is_retried_not_marked_done(server, vault):  # noqa: F811
    (vault / "Nutrition" / "Supplement Log.md").unlink()
    await server.supplement_nudges(datetime(2026, 11, 9, 18, 2, tzinfo=ZONE))
    assert server.hub_fake.calls == [] and "supplements" not in server.state
    (vault / "Nutrition" / "Supplement Log.md").write_text(LOG)
    await server.supplement_nudges(datetime(2026, 11, 9, 18, 10, tzinfo=ZONE))
    assert len(server.hub_fake.calls) == 1
