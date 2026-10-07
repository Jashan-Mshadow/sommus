"""What Sommus says first, without being asked: the morning brief and the nudges.

All plain code from the schedule file, the to-do list, Open-Meteo and the mailbox — no model, so a brief
every morning costs $0. The server's scheduler (server.py) decides when; this decides what.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from sommus.brain import campus

# `search_email` lines: [57604] • "Indeed" <donotreply@…> — Subject (Tue, 06 Oct 2026 …)
MAIL_LINE = re.compile(r'^\[\d+\]\s*•?\s*"?(?P<sender>[^"<]*?)"?\s*<[^>]*>\s*—\s*(?P<subject>.*?)\s*\([^()]*\)\s*$')


def unread_summary(listing: str, shown: int = 3) -> str:
    """'3 new emails: Davison (ECE 190 groups); Indeed (…)' from a search_email listing; '' when none."""
    mails = [m for line in listing.splitlines() if (m := MAIL_LINE.match(line.strip()))]
    if not mails:
        return ""
    parts = [f"{m['sender'].strip() or 'someone'} ({m['subject'][:60]})" for m in mails[:shown]]
    more = f", and {len(mails) - shown} more" if len(mails) > shown else ""
    count = f"{len(mails)} new email{'s' if len(mails) != 1 else ''}"
    return f"{count}: " + "; ".join(parts) + more + "."


def morning(schedule: campus.Schedule, now: datetime, weather: str = "", mail: str = "", tasks: list[str] = ()) -> str:
    lines = [f"Morning. {now:%A, %B %-d}."]
    lines.append(campus.day_summary(schedule, now.date(), now))
    due = schedule.due_within(now, 3)
    if due:
        lines.append(campus.due_summary(schedule, now, 3))
    if weather:
        lines.append(weather)
    if mail:
        lines.append(mail)
    if tasks:
        plain = [re.sub(r"[*_`~]", "", t).strip() for t in tasks[:3]]  # the vault's Markdown reads as clutter here
        lines.append("Top of your list: " + "; ".join(plain) + ".")
    return "\n".join(lines)


def due_tomorrow(schedule: campus.Schedule, now: datetime) -> str:
    """The evening heads-up: what's due tomorrow, or '' when nothing is."""
    tomorrow = now.date() + timedelta(days=1)
    due = [d for d in schedule.due_within(now, 2) if d.due.date() == tomorrow]
    if not due:
        return ""
    items = "; ".join(f"{d.what}" + (f" at {campus.clock(d.due)}" if d.due.hour != 23 else "") for d in due)
    return f"Heads up, due tomorrow: {items}."


def class_nudges(schedule: campus.Schedule, now: datetime, minutes: int, sent: set[tuple[str, str]]) -> list[str]:
    """'ECE 105 lecture in 15 minutes, in E2 1792.' for each class starting within `minutes`, once each.
    `sent` remembers which ones went out, so a restart inside the window doesn't repeat them."""
    out = []
    for session in schedule.upcoming(now, days=1):
        ahead = (session.start - now).total_seconds() / 60
        key = (session.label, session.start.isoformat())
        if 0 < ahead <= minutes and key not in sent:
            sent.add(key)
            room = f", in {session.room}" if session.room and session.room != "TBA" else ""
            out.append(f"{session.label} {campus.until(session.start, now)}{room}.")
    return out


def due_at(clock_text: str, day: date, zone) -> datetime:
    hour, minute = (int(x) for x in clock_text.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
