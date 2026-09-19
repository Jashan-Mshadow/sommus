"""Campus engine: classes, rooms and deadlines answered by plain code — $0 and instant.

"Where's my next class" used to go through ask_claude, which read two Google calendars and took
20–27 s. The schedule barely changes, so it lives in a TOML file in the vault (weekly slots,
one-off sessions, breaks, deadlines) and this module turns it into answers and a `today.json`
that other tools (the morning brief) can read instead of re-deriving the day.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
KINDS = {"LEC": "lecture", "TUT": "tutorial", "LAB": "lab", "SEM": "seminar", "EXAM": "midterm"}
LOOKAHEAD_DAYS = 21  # "next class" searches this far ahead (covers reading week)


@dataclass(frozen=True)
class Session:
    course: str
    kind: str
    start: datetime
    end: datetime | None
    room: str
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.course} {KINDS.get(self.kind, self.kind.lower())}"

    def to_dict(self) -> dict:
        return {
            "course": self.course,
            "kind": KINDS.get(self.kind, self.kind.lower()),
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "room": self.room,
            "note": self.note,
        }


@dataclass(frozen=True)
class Deadline:
    what: str
    due: datetime
    where: str = ""


def _in(day: date, spans: list[dict]) -> bool:
    return any(span["from"] <= day <= span["to"] for span in spans)


class Schedule:
    def __init__(self, raw: dict):
        self.zone = ZoneInfo(raw.get("timezone", "America/Toronto"))
        self.term_start: date = raw["term_start"]
        self.term_end: date = raw["term_end"]
        self.breaks: list[dict] = raw.get("breaks", [])
        self.weekly: list[dict] = raw.get("weekly", [])
        self.once: list[dict] = raw.get("once", [])
        self.deadlines = sorted(
            (
                Deadline(d["what"], d["due"].replace(tzinfo=self.zone), d.get("where", ""))
                for d in raw.get("deadlines", [])
            ),
            key=lambda d: d.due,
        )

    @classmethod
    def load(cls, path: Path) -> Schedule:
        return cls(tomllib.loads(path.read_text()))

    def _at(self, day: date, clock: time | None) -> datetime | None:
        return datetime.combine(day, clock, self.zone) if clock else None

    def _session(self, slot: dict, day: date) -> Session:
        return Session(
            slot["course"],
            slot["kind"],
            self._at(day, slot["start"]),
            self._at(day, slot.get("end")),
            slot.get("room", ""),
            slot.get("note", ""),
        )

    def on(self, day: date) -> list[Session]:
        """Every session on one day, in order."""
        found = [self._session(slot, day) for slot in self.once if slot["date"] == day]
        in_term = self.term_start <= day <= self.term_end and not _in(day, self.breaks)
        if in_term:
            for slot in self.weekly:
                if DAYS.index(slot["day"]) != day.weekday():
                    continue
                if slot.get("first") and day < slot["first"] or slot.get("last") and day > slot["last"]:
                    continue
                if _in(day, slot.get("skip", [])):
                    continue
                found.append(self._session(slot, day))
        return sorted(found, key=lambda s: s.start)

    def current(self, now: datetime) -> Session | None:
        return next((s for s in self.on(now.date()) if s.start <= now < (s.end or s.start + timedelta(hours=1))), None)

    def upcoming(self, now: datetime, days: int = LOOKAHEAD_DAYS) -> list[Session]:
        found = []
        for offset in range(days + 1):
            found += [s for s in self.on(now.date() + timedelta(days=offset)) if s.start > now]
        return found

    def due_within(self, now: datetime, days: int = 7) -> list[Deadline]:
        return [d for d in self.deadlines if now <= d.due <= now + timedelta(days=days)]


DEFAULT_SCHEDULE = "~/Documents/Jashans_Brain/Courses/schedule.toml"
DEFAULT_TODO = "~/Documents/Jashans_Brain/TODO.md"
_cache: dict[Path, tuple[float, Schedule]] = {}


def schedule_path() -> Path:
    from sommus import config

    return Path(config.section("campus").get("schedule", DEFAULT_SCHEDULE)).expanduser()


def todo_path() -> Path:
    from sommus import config

    return Path(config.section("campus").get("todo", DEFAULT_TODO)).expanduser()


def cached(path: Path) -> Schedule:
    """Parse once; re-read only when the file changes."""
    mtime = path.stat().st_mtime
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    schedule = Schedule.load(path)
    _cache[path] = (mtime, schedule)
    return schedule


# ---------------------------------------------------------------- answers that sound like a person


def clock(moment: datetime) -> str:
    return f"{moment:%-I:%M %p}".replace(":00 ", " ")


def say_day(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    if 0 < (day - today).days < 7:
        return day.strftime("%A")
    return day.strftime("%A, %B %-d")


def until(moment: datetime, now: datetime) -> str:
    minutes = round((moment - now).total_seconds() / 60)
    if minutes < 60:
        return f"in {minutes} minute{'s' if minutes != 1 else ''}"
    hours, rest = divmod(minutes, 60)
    if rest < 10:
        return f"in {hours} hour{'s' if hours != 1 else ''}"
    return f"in {hours}h {rest}m"


def _room(session: Session) -> str:
    return f" in {session.room}" if session.room and session.room != "TBA" else ""


def _line(session: Session) -> str:
    note = f" ({session.note})" if session.note else ""
    return f"{clock(session.start)} {session.label}{_room(session)}{note}"


def next_class(schedule: Schedule, now: datetime) -> str:
    ahead = schedule.upcoming(now)
    now_in = schedule.current(now)
    lead = f"You're in {now_in.label} until {clock(now_in.end)}. " if now_in and now_in.end else ""
    if not ahead:
        return lead + "No classes in the next three weeks."
    nxt = ahead[0]
    when = say_day(nxt.start.date(), now.date())
    if when == "today":
        return f"{lead}Next is {nxt.label} at {clock(nxt.start)}{_room(nxt)}, {until(nxt.start, now)}."
    return f"{lead}Your next class is {when} at {clock(nxt.start)}: {nxt.label}{_room(nxt)}."


def day_summary(schedule: Schedule, day: date, now: datetime) -> str:
    sessions = schedule.on(day)
    when = say_day(day, now.date())
    if day == now.date():
        left = [s for s in sessions if (s.end or s.start) > now]
        if not sessions:
            return "No classes today. " + next_class(schedule, now)
        if not left:
            return "You're done for today. " + next_class(schedule, now)
        now_in = schedule.current(now)
        rest = [s for s in left if s != now_in]
        if now_in and now_in.end:
            head = f"You're in {now_in.label} until {clock(now_in.end)}. "
            if not rest:
                return head + "That's your last one today."
            return head + "After that: " + "; ".join(_line(s) for s in rest) + "."
        many = f"{len(rest)} class{'es' if len(rest) != 1 else ''}"
        count = f"{many} today" if len(rest) == len(sessions) else f"{len(rest)} more today"
        return f"{count[0].upper()}{count[1:]}: " + "; ".join(_line(s) for s in rest) + "."
    if not sessions:
        return f"No classes {when}."
    last = sessions[-1]
    finish = f" Done at {clock(last.end)}." if last.end else ""
    return f"{when[0].upper()}{when[1:]}: " + "; ".join(_line(s) for s in sessions) + "." + finish


def due_summary(schedule: Schedule, now: datetime, days: int = 7) -> str:
    due = schedule.due_within(now, days)
    if not due:
        return f"Nothing due in the next {days} days."
    parts = []
    for d in due:
        when = say_day(d.due.date(), now.date())
        at = "" if d.due.time() == time(23, 59) else f" at {clock(d.due)}"
        parts.append(f"{d.what}, {when}{at}")
    return "Due soon: " + "; ".join(parts) + "."


# ---------------------------------------------------------------- today.json


TASK = re.compile(r"^\s*- \[ \] (?:\d+\.\s*)?(.+)$")


def open_tasks(todo: Path, limit: int = 15) -> list[str]:
    """Unchecked items from the first section of the TODO note (the ranked list for right now)."""
    if not todo.exists():
        return []
    tasks, sections = [], 0
    for line in todo.read_text(errors="replace").splitlines():
        if line.startswith("## "):
            sections += 1
            if sections > 1:
                break
        elif sections == 1 and (m := TASK.match(line)):
            tasks.append(re.sub(r"\[\[([^]|]+)(\|[^]]+)?]]", r"\1", m.group(1)).strip())
    return tasks[:limit]


def today(schedule: Schedule, now: datetime, todo: Path | None = None) -> dict:
    ahead = schedule.upcoming(now)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "date": now.date().isoformat(),
        "classes_today": [s.to_dict() for s in schedule.on(now.date())],
        "next_class": ahead[0].to_dict() if ahead else None,
        "classes_tomorrow": [s.to_dict() for s in schedule.on(now.date() + timedelta(days=1))],
        "deadlines_7d": [
            {"what": d.what, "due": d.due.isoformat(), "where": d.where} for d in schedule.due_within(now, 7)
        ],
        "open_tasks": open_tasks(todo) if todo else [],
    }


def write_today(schedule: Schedule, now: datetime, out: Path, todo: Path | None = None) -> dict:
    """Write today.json for other tools (the morning brief) to read instead of re-deriving the day."""
    data = today(schedule, now, todo)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2) + "\n")
    return data


# ---------------------------------------------------------------- which question was asked

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _asked_day(tokens: list[str], today: date) -> date | None:
    if {"tomorrow", "tomorrows"} & set(tokens):
        return today + timedelta(days=1)
    for name in WEEKDAYS:
        if name in tokens:
            return today + timedelta(days=(WEEKDAYS.index(name) - today.weekday()) % 7)
    if {"today", "todays", "left", "more", "rest", "else"} & set(tokens):
        return today
    return None


def first_class(schedule: Schedule, day: date, now: datetime) -> str:
    sessions = schedule.on(day)
    when = say_day(day, now.date())
    if not sessions:
        return f"No classes {when}."
    first = sessions[0]
    return f"Your first class {when} is {first.label} at {clock(first.start)}{_room(first)}."


def answer(schedule: Schedule, tokens: list[str], now: datetime) -> str:
    """'where's my next class' / 'classes tomorrow' / 'what's left today' / 'first class monday'."""
    day = _asked_day(tokens, now.date())
    if "first" in tokens:
        return first_class(schedule, day or now.date(), now)
    if day is not None:
        return day_summary(schedule, day, now)
    if {"next", "where", "when"} & set(tokens):
        return next_class(schedule, now)
    return day_summary(schedule, now.date(), now)


def due_answer(schedule: Schedule, tokens: list[str], now: datetime) -> str:
    """'what's due this week' / 'anything due tomorrow' / 'deadlines'."""
    day = _asked_day([t for t in tokens if t not in {"left", "more", "rest", "else"}], now.date())
    if day is None:
        return due_summary(schedule, now)
    due = [d for d in schedule.due_within(now, (day - now.date()).days + 1) if d.due.date() == day]
    when = say_day(day, now.date())
    if not due:
        return f"Nothing due {when}."
    return (
        f"Due {when}: "
        + "; ".join(d.what + ("" if d.due.time() == time(23, 59) else f" at {clock(d.due)}") for d in due)
        + "."
    )
