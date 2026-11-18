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


# ---- "I took my creatine": logged by code, no model, no PIN ----

STACK = ["multivitamin", "hair gummies", "bodyarmor", "fish oil", "k2+d3", "creatine", "magnesium"]


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("took my creatine", ["creatine"]),
        ("Hair gummy and multi consumed", ["hair gummies", "multivitamin"]),
        ("hey sommus i took fish oil and k2", ["fish oil", "k2+d3"]),
        ("Everything had except creatine and magnesium", STACK[:5]),
        ("had everything with dinner", STACK),
        # questions and anything not fully understood are left to the model
        ("did I take creatine?", None),
        ("what is creatine", None),
        ("should I take magnesium tonight", None),
        ("I had fiber one cereal", None),
        ("I ate a banana", None),
        ("play some music", None),
    ],
)
def test_a_report_of_what_he_took_is_understood_only_when_fully_understood(said, expected):
    assert supplements.parse_report(said, STACK) == expected


@pytest.fixture
def log_note(tmp_path, monkeypatch):
    from sommus.nodes.vault import server as vault

    monkeypatch.setattr(vault, "VAULT", tmp_path)
    note = tmp_path / "Nutrition" / "Supplement Log.md"
    note.parent.mkdir(parents=True)
    note.write_text(LOG)
    return vault, note


def test_log_supplements_appends_rows_skips_repeats_and_says_what_is_left(log_note):
    vault, note = log_note
    today = datetime.now().date().isoformat()
    said = vault.log_supplements("creatine, magnesium", scheduled="creatine, magnesium, k2+d3")
    assert said == "Logged creatine and magnesium. Still to take today: K2 and D3."
    rows = [line for line in note.read_text().splitlines() if line.startswith(f"| {today}")]
    assert [r.split("|")[3].strip() for r in rows] == ["creatine", "magnesium"]
    assert note.read_text().endswith("|\n")  # the table stays the last thing in the file

    again = vault.log_supplements("creatine", scheduled="creatine, magnesium, k2+d3")
    assert again.startswith("Those were already logged today.")
    assert note.read_text().count("| creatine |") == 1


def test_log_supplements_refuses_a_name_it_does_not_know(log_note):
    vault, note = log_note
    before = note.read_text()
    with pytest.raises(vault.VaultError, match="Unknown supplement"):
        vault.log_supplements("unicorn dust")
    assert note.read_text() == before


def test_log_food_adds_a_line_and_says_the_macros_are_not_totalled(tmp_path, monkeypatch):
    from sommus.nodes.vault import server as vault

    monkeypatch.setattr(vault, "VAULT", tmp_path)
    said = vault.log_food("mac and cheese bowl v2, 1 kiwi")
    assert "macros aren't totalled" in said
    day = tmp_path / "Nutrition" / "Logs" / f"{datetime.now().date().isoformat()}.md"
    assert "mac and cheese bowl v2, 1 kiwi" in day.read_text() and day.read_text().startswith("---")
    vault.log_food("a banana")
    assert day.read_text().count("ate (noted by Sommus") == 2
