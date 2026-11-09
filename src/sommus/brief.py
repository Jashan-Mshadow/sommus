"""What Sommus says first, without being asked: the morning brief and the nudges.

All plain code from the schedule file, the to-do list, Open-Meteo and the mailbox — no model, so a brief
every morning costs $0. The server's scheduler (server.py) decides when; this decides what.
"""

from __future__ import annotations

import random
import re
from collections.abc import Sequence
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


def morning(
    schedule: campus.Schedule, now: datetime, weather: str = "", mail: str = "", tasks: list[str] = (),
    gym: str = "", events: Sequence[str] = (), quip: str = "",
) -> str:  # fmt: skip
    """The written brief: classes, what's due soon, the gym, today's other events, weather, mail, the top of the
    to-do list, and one line of personality. The workout itself isn't here: it's in the vault's Training Hub."""
    lines = [f"Morning. {now:%A, %B %-d}."]
    lines.append(campus.day_summary(schedule, now.date(), now))
    due = schedule.due_within(now, 3)
    if due:
        lines.append(campus.due_summary(schedule, now, 3))
    if events:
        lines.append("Also today: " + "; ".join(events) + ".")
    lines.append(f"Gym today: {gym}." if gym else "Rest day, no gym.")
    if weather:
        lines.append(weather)
    if mail:
        lines.append(mail)
    if tasks:
        plain = [re.sub(r"[*_`~]", "", t).strip() for t in tasks[:3]]  # the vault's Markdown reads as clutter here
        lines.append("Top of your list: " + "; ".join(plain) + ".")
    if quip:
        lines.append(quip)
    return "\n".join(lines)


DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def gym_today(settings: dict, day: date) -> str:
    """'Day 1' on a gym day from [gym] days in config.toml, '' on a rest day."""
    return str(settings.get("days", {}).get(DAYS[day.weekday()], ""))


GYM_QUIPS = (
    "Chest first, excuses never.",
    "The lateral raises won't raise themselves.",
    "Go make the cable station regret it.",
    "Eat your protein, then go earn it.",
)
REST_QUIPS = (
    "Recover like it's your job. Which, today, it is.",
    "The only thing getting a pump today is your problem set.",
    "Walk, eat, study. Riveting, I know.",
    "Muscles grow on rest days. Allegedly. Don't test it on the couch all day.",
)
BUSY_QUIPS = ("Big day. Pace yourself.", "Plenty due, so maybe skip the second episode tonight.")


def morning_quip(gym: str, due_count: int, book: Phrasebook) -> str:
    if due_count >= 2:
        return book.pick("quip-busy", BUSY_QUIPS)
    return book.pick("quip-gym", GYM_QUIPS) if gym else book.pick("quip-rest", REST_QUIPS)


def due_tomorrow(schedule: campus.Schedule, now: datetime) -> str:
    """The evening heads-up: what's due tomorrow, or '' when nothing is."""
    tomorrow = now.date() + timedelta(days=1)
    due = [d for d in schedule.due_within(now, 2) if d.due.date() == tomorrow]
    if not due:
        return ""
    items = "; ".join(f"{d.what}" + (f" at {campus.clock(d.due)}" if d.due.hour != 23 else "") for d in due)
    return f"Heads up, due tomorrow: {items}."


def classes_starting(
    schedule: campus.Schedule, now: datetime, minutes: int, sent: set[tuple[str, str]]
) -> list[campus.Session]:
    """Each class starting within `minutes`, once each. `sent` remembers which ones went out, so a restart
    inside the window doesn't repeat them."""
    out = []
    for session in schedule.upcoming(now, days=1):
        ahead = (session.start - now).total_seconds() / 60
        key = (session.label, session.start.isoformat())
        if 0 < ahead <= minutes and key not in sent:
            sent.add(key)
            out.append(session)
    return out


def recent_nudges(sent: set[tuple[str, str]], now: datetime, keep_hours: int = 24) -> list[list[str]]:
    """The nudge keys still worth remembering: classes that started within `keep_hours`, or haven't yet."""
    cutoff = now - timedelta(hours=keep_hours)
    kept = []
    for label, start in sent:
        try:
            when = datetime.fromisoformat(start)
        except ValueError:
            continue
        if when.tzinfo is None or when >= cutoff:
            kept.append([label, start])
    return sorted(kept, key=lambda k: k[1])


def class_nudge(session: campus.Session, now: datetime) -> str:
    """'ECE 105 lecture in 15 minutes, in E2 1792.' — the written one, codes and all."""
    return f"{session.label} {campus.until(session.start, now)}{_room(session, ', in ')}."


def class_nudges(schedule: campus.Schedule, now: datetime, minutes: int, sent: set[tuple[str, str]]) -> list[str]:
    return [class_nudge(s, now) for s in classes_starting(schedule, now, minutes, sent)]


def _room(session: campus.Session, lead: str) -> str:
    return f"{lead}{session.room}" if session.room and session.room != "TBA" else ""


def due_at(clock_text: str, day: date, zone) -> datetime:
    hour, minute = (int(x) for x in clock_text.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)


def reminders_today(reminders, now: datetime) -> str:
    """'Reminders today: Take creatine at 9:30 PM; Call the bank.' — timed ones due today, plus untimed ones."""
    parts = []
    for r in reminders:
        when = r.when()
        if when is None:
            parts.append(r.title)
        elif when.date() == now.date():
            parts.append(f"{r.title} at {campus.clock(when)}")
    return "Reminders today: " + "; ".join(parts[:5]) + "." if parts else ""


# ---------------------------------------------------------------- said out loud on the Mac
#
# The written lines above are for a screen: codes, rooms, times. Spoken, the same news is shorter and in words
# people use ("Physics in fifteen, room RCH 103."), and it changes its phrasing so the third nudge of the day
# doesn't sound like the first. Still $0: templates, no model.

ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen").split()  # fmt: skip
TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}


def words(n: int) -> str:
    """15 -> 'fifteen', 42 -> 'forty-two'. Numbers past 59 stay digits: nothing here says them."""
    if 0 <= n < 20:
        return ONES[n]
    if 20 <= n < 60:
        tens, ones = divmod(n, 10)
        return TENS[tens] + (f"-{ONES[ones]}" if ones else "")
    return str(n)


class Phrasebook:
    """Picks one of a few phrasings, never the one it used last time for the same kind of line."""

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.last: dict[str, int] = {}

    def pick(self, kind: str, options: Sequence[str]) -> str:
        choices = [i for i in range(len(options)) if i != self.last.get(kind)] or [0]
        index = self.rng.choice(choices)
        self.last[kind] = index
        return options[index]


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _lower(text: str) -> str:
    """'Take creatine' -> 'take creatine', but 'ECE 105' and 'iPhone' stay as they are."""
    return text[:1].lower() + text[1:] if text[1:2].islower() else text


def subject(session: campus.Session, say_as: dict[str, str]) -> str:
    """'ECE 105 lecture' -> 'physics lecture'; a lecture is the default, so it's just 'physics'."""
    from sommus.interfaces.voice import say_courses

    said = say_courses(session.label, say_as)
    return re.sub(r"\s+lecture$", "", said) if said != session.label else said


CLASS_SOON = (
    "{Subject} in {n}{room_comma}.",
    "{Subject} starts in {n} minutes{room_in}.",
    "Heads up, {subject} in {n} minutes.{room_its}",
    "{N} minutes to {subject}{room_comma}.",
)


def spoken_class_nudge(session: campus.Session, now: datetime, say_as: dict[str, str], book: Phrasebook) -> str:
    name = subject(session, say_as)
    n = words(max(1, round((session.start - now).total_seconds() / 60)))
    return book.pick("class", CLASS_SOON).format(
        Subject=_cap(name), subject=name, n=n, N=_cap(n),
        room_comma=_room(session, ", room "), room_in=_room(session, ", in "),
        room_its=_room(session, " It's in ") + ("." if _room(session, "x") else ""),
    )  # fmt: skip


REMINDER = ("Reminder: {title}.", "Quick reminder, {title}.", "Don't forget, {title}.", "Just a nudge: {title}.")
TIMER = ("Your {what} timer is up.", "That's {what}, timer's done.", "Time's up on the {what} timer.")
TIMER_TITLE = re.compile(r"^timer\s*\(\s*(\d+)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hours?)\s*\)$", re.I)
UNITS = {"s": "second", "m": "minute", "h": "hour"}


def spoken_reminder(title: str, book: Phrasebook, late_for: datetime | None = None) -> str:
    title = title.strip().rstrip(".")
    if timer := TIMER_TITLE.match(title):
        amount, unit = int(timer[1]), UNITS[timer[2][0].lower()]
        line = book.pick("timer", TIMER).format(what=f"{words(amount)} {unit}")
    else:
        line = book.pick("reminder", REMINDER).format(title=_lower(title))
    if late_for:
        line += f" That was meant for {campus.clock(late_for)}."
    return line


DUE_TOMORROW = (
    "Just so you know, {items} {is} due tomorrow.",
    "Heads up, tomorrow you've got {items} due.",
    "Don't forget, {items} {is} due tomorrow.",
)


def spoken_due_tomorrow(schedule: campus.Schedule, now: datetime, say_as: dict[str, str], book: Phrasebook) -> str:
    from sommus.interfaces.voice import say_courses

    tomorrow = now.date() + timedelta(days=1)
    due = [say_courses(d.what, say_as) for d in schedule.due_within(now, 2) if d.due.date() == tomorrow]
    if not due:
        return ""
    items = due[0] if len(due) == 1 else ", ".join(due[:-1]) + " and " + due[-1]
    return book.pick("due", DUE_TOMORROW).format(items=items, **{"is": "is" if len(due) == 1 else "are"})


HELLO = ("Good morning{name}.", "Morning{name}.", "Morning{name}, hope you slept well.")


def spoken_morning(
    schedule: campus.Schedule, now: datetime, say_as: dict[str, str], book: Phrasebook, name: str = "",
    weather: str = "", gym: str = "",
) -> str:  # fmt: skip
    """The greeting when the Mac first wakes in the morning: the first class and what's due today. The full
    brief is on Telegram; this is the part worth hearing."""
    from sommus.interfaces.voice import say_courses

    parts = [book.pick("hello", HELLO).format(name=f", {name}" if name else "")]
    left = [s for s in schedule.on(now.date()) if s.start > now]
    if not left:
        parts.append("No more classes today." if schedule.on(now.date()) else "No classes today.")
    else:
        first = left[0]
        more = len(left) - 1
        after = f", then {words(more)} more" if more else ", and that's the only one"
        parts.append(f"First up is {subject(first, say_as)} at {campus.clock(first.start)}{after}.")
    due = [say_courses(d.what, say_as) for d in schedule.due_within(now, 1) if d.due.date() == now.date()]
    if due:
        parts.append(f"{_cap(' and '.join(due))} {'is' if len(due) == 1 else 'are'} due today.")
    if gym:
        parts.append(f"Gym today, {gym}.")
    if weather:
        parts.append(weather)
    return " ".join(parts)
