"""Supplement nudges: at each slot, check what the vault says he has taken today and speak up only about
what is missing.

The schedule is `[supplements]` in config.toml. The record is the vault note `Nutrition/Supplement Log.md`, an
append-only table (date | time | item | note) that Jashan, Sommus or Claude append to. Plain code, no model:
the server's scheduler (server.py) decides when, this decides whether and what.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta

from sommus import brief

ALIASES = {
    "multivitamin": ("multivitamin", "multi", "mv", "multivitamins", "vitamins"),
    "hair gummies": ("hair gummies", "hair gummy", "hair", "gummies", "hair/skin/nails", "hair skin nails", "nails"),
    "fish oil": ("fish oil", "omega-3", "omega 3", "omega3", "omega"),
    "k2+d3": ("k2+d3", "d3+k2", "k2 d3", "d3 k2", "k2", "d3", "vitamin d", "d2k3", "k2d3"),
    "creatine": ("creatine",),
    "magnesium": ("magnesium", "magnesium glycinate", "mag", "mg"),
    "bodyarmor": ("bodyarmor", "body armor", "body armour", "lyte", "bodylyte", "bodyarmor lyte"),
    "fiber gummy": ("fiber gummy", "fiber gummies", "fibre gummy", "fiber"),
}
LOOKUP = {alias: canon for canon, names in ALIASES.items() for alias in names}
WRITTEN = {"k2+d3": "K2+D3", "bodyarmor": "BodyArmor Lyte", "hair gummies": "hair/skin/nails gummies"}
SPOKEN = {"k2+d3": "K2 and D3", "bodyarmor": "body armor", "hair gummies": "hair gummies"}


def canon(name: str) -> str:
    """'Omega-3' -> 'fish oil'; names nobody listed stay as typed (lowercased)."""
    key = " ".join(name.lower().split())
    return LOOKUP.get(key, key)


def taken_on(log_text: str, day: date) -> set[str]:
    """Canonical names of what the log's table says was taken on `day`."""
    wanted, taken = day.isoformat(), set()
    for line in log_text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and line.lstrip().startswith("|") and cells[0] == wanted and cells[2]:
            taken.add(canon(cells[2]))
    return taken


def missing(items: Iterable[str], taken: set[str]) -> list[str]:
    return [c for item in items if (c := canon(item)) not in taken]


def slots_due(slots: Sequence[dict], now: datetime, window_minutes: int, done: set[str]) -> list[dict]:
    """Slots whose time has come (and not so long ago that nudging would be silly), once each per day.
    `done` holds 'YYYY-MM-DD|slot' keys already handled."""
    out = []
    for slot in slots:
        at = brief.due_at(slot["at"], now.date(), now.tzinfo)
        key = f"{now.date().isoformat()}|{slot['name']}"
        if key not in done and at <= now < at + timedelta(minutes=window_minutes):
            out.append(slot)
    return out


def key_for(slot: dict, now: datetime) -> str:
    return f"{now.date().isoformat()}|{slot['name']}"


def _join(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def written(slot: dict, lacking: Sequence[str], now: datetime) -> str:
    label = slot.get("say", slot["name"])
    return f"Supplements, {label}: {_join([WRITTEN.get(c, c) for c in lacking])} not logged yet."


NUDGES = (
    "Supplement check, {label}. Have you taken your {items}?",
    "{Label}. {Items}, taken yet?",
    "Quick one: {items}. Not logged yet.",
)


def spoken(slot: dict, lacking: Sequence[str], book: brief.Phrasebook) -> str:
    label = slot.get("say", slot["name"])
    items = _join([SPOKEN.get(c, c) for c in lacking])
    return book.pick("supplements", NUDGES).format(
        label=label, Label=label[:1].upper() + label[1:], items=items, Items=items[:1].upper() + items[1:]
    )
