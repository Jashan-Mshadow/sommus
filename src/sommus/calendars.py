"""Google Calendar on the always-on brain, without the laptop.

Calendar questions went to Claude Code on the Mac (`ask_claude`, ~20 s, and nothing while it slept). Every
Google calendar has a private iCal link (Settings → the calendar → "Secret address in iCal format"); with those in
the server's .env as CALENDAR_ICS_URLS (comma-separated), the server reads the events itself — repeating ones
included — in well under a second, for $0. Read-only: adding events still goes through the Mac for now.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sommus.brain import campus

# UW Flow's events look like "ECE150 - LEC 001". Its feed doesn't know reading week, so on the schedule file's
# breaks these are dropped (personal events stay).
CLASS_EVENT = re.compile(r"^[A-Z]{2,8}\s?\d{3}[A-Z]?\s+-\s+(?:LEC|TUT|LAB|SEM|TST|PRJ)\b")
CACHE_SECONDS = 600  # a calendar changes rarely; refetching each question would cost a second every time
ZONE = ZoneInfo("America/Toronto")


@dataclass(frozen=True)
class Event:
    title: str
    start: datetime
    end: datetime | None
    all_day: bool
    where: str = ""


def urls() -> list[str]:
    return [u.strip() for u in os.environ.get("CALENDAR_ICS_URLS", "").split(",") if u.strip()]


def parse(ics: bytes | str, start: datetime, end: datetime, zone=ZONE) -> list[Event]:
    import icalendar
    import recurring_ical_events

    events = []
    calendar = icalendar.Calendar.from_ical(ics)
    for item in recurring_ical_events.of(calendar).between(start, end):
        begin = item.get("DTSTART").dt
        finish = item.get("DTEND").dt if item.get("DTEND") else None
        all_day = not isinstance(begin, datetime)
        if all_day:
            begin = datetime(begin.year, begin.month, begin.day, tzinfo=zone)
            finish = None
        else:
            begin = begin.astimezone(zone) if begin.tzinfo else begin.replace(tzinfo=zone)
            finish = finish.astimezone(zone) if isinstance(finish, datetime) and finish.tzinfo else None
        title = str(item.get("SUMMARY", "")).strip() or "(no title)"
        events.append(Event(title, begin, finish, all_day, str(item.get("LOCATION", "")).strip()))
    return sorted(events, key=lambda e: (e.start, not e.all_day))


def say(events: list[Event], first: date, days: int, now: datetime) -> str:
    """'Tomorrow: 8:30 AM ECE 198 lab (E2 1792); 7 PM Hackathon info session.' — one sentence per day."""
    lines = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        today = [e for e in events if e.start.date() == day]
        if not today and days > 1:
            continue
        label = campus.say_day(day, now.date())
        label = label[0].upper() + label[1:]
        if not today:
            lines.append(f"{label}: nothing on your calendar.")
            continue
        parts = []
        for e in today:
            where = f" ({e.where})" if e.where else ""
            parts.append(f"{e.title}{where} (all day)" if e.all_day else f"{campus.clock(e.start)} {e.title}{where}")
        lines.append(f"{label}: " + "; ".join(parts) + ".")
    return " ".join(lines) or "Nothing on your calendar then."


class Calendars:
    def __init__(self, links: list[str]):
        self.links = links
        self._cache: dict[str, tuple[float, bytes]] = {}

    def _fetch(self, link: str) -> bytes:
        import httpx2

        held = self._cache.get(link)
        if held and time.monotonic() - held[0] < CACHE_SECONDS:
            return held[1]
        response = httpx2.get(link, timeout=15, follow_redirects=True)
        response.raise_for_status()
        body = response.content
        self._cache[link] = (time.monotonic(), body)
        return body

    def events(self, first: date, days: int, zone=ZONE) -> list[Event]:
        start = datetime(first.year, first.month, first.day, tzinfo=zone)
        end = start + timedelta(days=days)
        found: list[Event] = []
        for link in self.links:
            found += parse(self._fetch(link), start, end, zone)
        with contextlib.suppress(Exception):  # no schedule file: keep everything
            breaks = campus.cached(campus.schedule_path()).breaks
            found = [e for e in found if not (CLASS_EVENT.match(e.title) and campus._in(e.start.date(), breaks))]
        return sorted(found, key=lambda e: (e.start, not e.all_day))

    def node(self):
        from mcp.server.mcpserver import MCPServer
        from mcp.types import ToolAnnotations

        server = MCPServer("calendar", log_level="WARNING")

        @server.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
        def calendar_events(start: str = "today", days: int = 1) -> str:
            """Jashan's Google Calendar events (all his calendars), read on the server: works with the laptop asleep.

            Args:
                start: "today", "tomorrow", or a date like "2026-10-09".
                days: How many days from start (7 for a week).
            """
            now = datetime.now(ZONE)
            first = {"today": now.date(), "tomorrow": now.date() + timedelta(days=1)}.get(start.strip().lower())
            if first is None:
                try:
                    first = date.fromisoformat(start.strip())
                except ValueError:
                    return f"Couldn't read the date '{start}'. Use today, tomorrow or 2026-10-09."
            days = max(1, min(int(days), 31))
            return say(self.events(first, days), first, days, now)

        return server


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def asked_range(tokens: list[str], today: date) -> tuple[str, int]:
    """(start, days) for calendar_events from the words of a question: 'tomorrow', 'friday', 'this week'."""
    words = set(tokens)
    if "weekend" in words:
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        return saturday.isoformat(), 2
    if "week" in words and "next" in words:  # "next week": Monday to Sunday after this one
        monday = today + timedelta(days=7 - today.weekday())
        return monday.isoformat(), 7
    if "week" in words:
        return "today", 7
    if "tomorrow" in words or "tomorrows" in words:
        return "tomorrow", 1
    for i, name in enumerate(WEEKDAYS):
        if name in words:
            ahead = (i - today.weekday()) % 7
            return (today + timedelta(days=ahead)).isoformat(), 1
    return "today", 1
