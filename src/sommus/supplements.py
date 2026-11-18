"""Supplement nudges: at each slot, check what the vault says he has taken today and speak up only about
what is missing.

The schedule is `[supplements]` in config.toml. The record is the vault note `Nutrition/Supplement Log.md`, an
append-only table (date | time | item | note) that Jashan, Sommus or Claude append to. Plain code, no model:
the server's scheduler (server.py) decides when, this decides whether and what.
"""

from __future__ import annotations

import re
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


BACK_OFF_AFTER = 3  # days in a row an item was still unlogged at its nudge, then only the written one


def backed_off(misses: dict, lacking: Iterable[str], day: date) -> bool:
    """True when every lacking item went unlogged at its nudge on each of the last BACK_OFF_AFTER days: the
    spoken nudge is ignored, so only the written one goes out."""
    yesterday = (day - timedelta(days=1)).isoformat()
    lacking = list(lacking)
    return bool(lacking) and all(
        misses.get(c, {}).get("day") in (yesterday, day.isoformat()) and misses[c]["streak"] >= BACK_OFF_AFTER
        for c in lacking
    )


def update_misses(misses: dict, slot_items: Iterable[str], taken: set[str], day: date) -> None:
    """Count, per item, the days in a row it was unlogged at its nudge (once a day). Logged in time: reset."""
    today, yesterday = day.isoformat(), (day - timedelta(days=1)).isoformat()
    for item in slot_items:
        c = canon(item)
        if c in taken:
            misses.pop(c, None)
            continue
        last = misses.get(c, {})
        if last.get("day") == today:
            continue
        streak = last["streak"] + 1 if last.get("day") == yesterday else 1
        misses[c] = {"day": today, "streak": streak}


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


# ---- "I took my creatine": a report that is logged by plain code, with no model and no PIN ----
# (his own notes, an append-only row per item; 2026-11-10 a reply like this cost $0.06 and a PIN and nothing was saved)

_NOT_ALIASES = {"mg"}  # a unit ("500 mg"), not a supplement
_ALIAS = re.compile(
    r"(?<![a-z0-9+/])("
    + "|".join(re.escape(a) for a in sorted(LOOKUP, key=len, reverse=True) if a not in _NOT_ALIASES)
    + r")(?![a-z0-9+/])"
)
_MARKER = re.compile(r"\b(?:except|but|besides|without|apart from|not|no)\b")
REPORT_VERBS = {"had", "have", "took", "taken", "consumed", "ate", "eaten", "drank", "done", "logged", "log", "add"}
REPORT_FILLER = {
    "i", "ive", "id", "just", "my", "the", "a", "an", "and", "with", "some", "also", "then", "all", "everything",
    "except", "but", "besides", "without", "apart", "from", "not", "no", "yet", "today", "supplements", "supplement",
    "pills", "before", "after", "gym", "dinner", "lunch", "breakfast", "morning", "tonight", "night", "earlier", "now",
    "already", "it", "as", "down", "ok", "okay", "hey", "sommus", "please", "pls", "thanks", "this", "mark", "marked",
    "tick", "had", "took", "ate",
}  # fmt: skip
QUESTION_WORDS = {"what", "whats", "when", "why", "how", "should", "do", "does", "did", "is", "are", "can", "could",
                  "would", "which", "who", "whether"}  # fmt: skip


def parse_report(text: str, scheduled: Iterable[str]) -> list[str] | None:
    """The supplements a message says he took, as canonical names, or None when it isn't such a report.

    Claims only what it fully understands: every word must be a supplement, a "took it" verb or filler, and a
    question never counts ("did I take creatine?"). 'everything except creatine and magnesium' = the scheduled
    stack minus those two."""
    raw = text.lower().replace("\u2019", "").replace("'", "")
    if "?" in raw:
        return None
    tokens = re.findall(r"[a-z]+", raw)
    if not tokens or tokens[0] in QUESTION_WORDS:
        return None
    marker = _MARKER.search(raw)
    before, after = (raw[: marker.start()], raw[marker.start() :]) if marker else (raw, "")
    named = [LOOKUP[m.group(1)] for m in _ALIAS.finditer(before)]
    excluded = {LOOKUP[m.group(1)] for m in _ALIAS.finditer(after)}
    rest = re.findall(r"[a-z]+", _ALIAS.sub(" ", raw))
    if not set(rest) <= REPORT_FILLER | REPORT_VERBS or not set(rest) & REPORT_VERBS:
        return None
    if {"everything", "all"} & set(rest):
        stack = [canon(x) for x in scheduled]
        found = [c for c in dict.fromkeys(stack + named) if c not in excluded]
    else:
        found = [c for c in dict.fromkeys(named) if c not in excluded]
    return found or None


def say_names(names: Sequence[str]) -> str:
    return _join([SPOKEN.get(c, c) for c in names])
