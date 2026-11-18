"""'How much has this cost in the past hour?': read the turn log, in plain code, free.

The numbers are what the brain's SQLite log holds, which is Claude (and its helpers). Gemini Live voice runs on
a free key and shows as $0, and the always-on server and the Mac keep separate logs, so this is the brain that
answered the question.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "sixty": 60,
}  # fmt: skip
UNITS = {
    "minute": "minutes", "minutes": "minutes", "min": "minutes", "mins": "minutes",
    "hour": "hours", "hours": "hours", "hr": "hours", "hrs": "hours",
    "day": "days", "days": "days", "week": "weeks", "weeks": "weeks", "month": "months", "months": "months",
}  # fmt: skip
COST_WORDS = {"cost", "costs", "spent", "spend", "spending", "bill", "billed", "charged", "usage", "price"}
FILLER = {
    "how", "much", "what", "whats", "is", "has", "have", "did", "does", "do", "the", "a", "sommus", "this", "that",
    "conversation", "so", "far", "in", "over", "for", "last", "past", "within", "total", "my", "me", "i", "it",
    "to", "please", "pls", "money", "dollars", "hey", "ok", "okay", "been", "since", "up", "of", "ago", "all",
    "many", "show", "tell", "give", "now", "running", "cost", "costs", "spent", "spend", "spending", "bill",
    "billed", "charged", "usage", "price", "today", "yesterday", "week", "month", "ever", "time", "on", "at",
}  # fmt: skip


def parse(text: str, now: datetime) -> tuple[datetime, str] | None:
    """(start of the window, how to say it) for a spending question, or None when it isn't one.
    Claims only a message made entirely of spending words, numbers and time units."""
    tokens = re.findall(r"[a-z]+|\d+", text.lower().replace("'", ""))
    if not tokens or not COST_WORDS & set(tokens):
        return None
    allowed = FILLER | set(NUMBERS) | set(UNITS)
    if any(t not in allowed and not t.isdigit() for t in tokens):
        return None
    for i, token in enumerate(tokens):
        if token in UNITS:
            before = tokens[i - 1] if i else ""
            if before == "this" and token in ("week", "month"):
                continue  # "this week" is the calendar week, handled below
            count = int(before) if before.isdigit() else NUMBERS.get(before, 1)
            unit = UNITS[token]
            span = {"minutes": timedelta(minutes=count), "hours": timedelta(hours=count), "days": timedelta(days=count),
                    "weeks": timedelta(weeks=count), "months": timedelta(days=30 * count)}[unit]  # fmt: skip
            label = f"the past {unit[:-1] if count == 1 else f'{count} {unit}'}"
            return now - span, label
    if "yesterday" in tokens:
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, "yesterday and today"
    if "week" in tokens:
        return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0), "this week"
    if "month" in tokens:
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), "this month"
    if {"ever", "total", "far"} & set(tokens) and "today" not in tokens:
        return datetime(2000, 1, 1), "all time"
    return now.replace(hour=0, minute=0, second=0, microsecond=0), "today"


def say_money(dollars: float) -> str:
    if dollars <= 0:
        return "nothing"
    if dollars < 0.01:
        return "under a cent"
    return f"{dollars * 100:.1f} cents" if dollars < 1 else f"${dollars:.2f}"


def say(window: str, requests: int, fast: int, dollars: float) -> str:
    if not requests:
        return f"No requests in {window}, so nothing spent."
    free = f", {fast} of them free on the fast path" if fast else ""
    return f"In {window}: {say_money(dollars)} across {requests} request{'s' if requests != 1 else ''}{free}."
