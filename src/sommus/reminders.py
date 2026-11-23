"""Reminders and timers kept by the always-on brain, so they work with the laptop closed.

Apple's Reminders app only exists on the Mac, and `create_reminder` failed whenever it slept. These live on
the server (data/reminders.json) and the scheduler in server.py pings Telegram when one is due — Telegram is on
his phone, so it reaches him anywhere. A reminder without a time waits in the list and shows in the morning
brief.

A reminder can also carry an action: "turn off the lamp in 5 minutes" is a reminder titled "Lamp off" whose
`action` is a plug_off call. At its time the scheduler runs the call instead of pinging, and tells Telegram
what happened.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

LATE_GRACE = timedelta(hours=12)  # a reminder missed while the server was down still goes out, marked late
ACTION_GRACE = timedelta(minutes=30)  # an action that late is skipped: the lamp going off hours later is wrong
PLUG_TOOLS = {True: "plug_on", False: "plug_off"}


@dataclass
class Reminder:
    id: str
    title: str
    due: str | None  # "2026-10-07 21:30", local time; None = whenever
    created: str
    done: bool = False
    action: dict[str, Any] | None = None  # {"tool": "plug_off", "args": {...}}: run at `due` instead of a ping

    def when(self) -> datetime | None:
        return datetime.strptime(self.due, "%Y-%m-%d %H:%M") if self.due else None


def parse_due(due: str | None) -> str | None:
    """Accepts '2026-10-07 21:30' or '2026-10-07T21:30'; raises ValueError on anything else."""
    if not due:
        return None
    return datetime.fromisoformat(due.strip().replace(" ", "T")).strftime("%Y-%m-%d %H:%M")


class Reminders:
    def __init__(self, path: Path):
        self.path = path
        self.items: list[Reminder] = []
        with contextlib.suppress(OSError, ValueError, TypeError):
            self.items = [Reminder(**r) for r in json.loads(path.read_text())]

    def save(self) -> None:
        self.items = [r for r in self.items if not r.done][-200:]  # finished ones go; the log has them
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps([asdict(r) for r in self.items], indent=1))

    def add(self, title: str, due: str | None, now: datetime, action: dict[str, Any] | None = None) -> Reminder:
        reminder = Reminder(uuid.uuid4().hex[:6], title.strip(), parse_due(due), f"{now:%Y-%m-%d %H:%M}", action=action)
        self.items.append(reminder)
        self.save()
        return reminder

    def open(self) -> list[Reminder]:
        return sorted((r for r in self.items if not r.done), key=lambda r: (r.due is None, r.due or ""))

    def take_due(self, now: datetime) -> list[tuple[Reminder, bool]]:
        """The reminders to send now, each with whether it's late; they're marked done."""
        fired = []
        for r in self.items:
            when = r.when()
            if r.done or when is None or when > now:
                continue
            r.done = True
            if now - when <= (ACTION_GRACE if r.action else LATE_GRACE):
                fired.append((r, now - when > timedelta(minutes=5)))
        if fired:
            self.save()
        return fired

    def cancel(self, which: str) -> list[Reminder]:
        which = which.strip().lower()
        hits = [r for r in self.open() if r.id == which or (which and which in r.title.lower())]
        for r in hits:
            r.done = True
        if hits:
            self.save()
        return hits

    def node(self, clock=datetime.now):
        from mcp.server.mcpserver import MCPServer
        from mcp.types import ToolAnnotations

        server = MCPServer("reminders", log_level="WARNING")
        change = ToolAnnotations(read_only_hint=False, destructive_hint=False)

        @server.tool(annotations=change, structured_output=False)
        def set_reminder(title: str, due: str | None = None) -> str:
            """Set a reminder or a timer. Use this, not create_reminder: it works with the laptop closed and
            pings Jashan on Telegram (his phone) at the time.

            Args:
                title: What to remind him of, e.g. "Take creatine", or "Timer (10 min)" for a timer.
                due: Local time as "2026-10-07 21:30". Omit for a reminder with no time.
            """
            try:
                reminder = self.add(title, due, clock())
            except ValueError:
                return f"Couldn't read the time '{due}'. Use 2026-10-07 21:30."
            return f"Reminder set: {reminder.title}" + (f" at {reminder.due}." if reminder.due else " (no time).")

        @server.tool(annotations=change, structured_output=False)
        def schedule_plug(on: bool, due: str, plug: str = "") -> str:
            """Switch the SwitchBot plug (the lamp at home) on or off LATER, e.g. "turn off the lamp in 5
            minutes" or "lamp on at 7 am". Runs on the server, so it works with the laptop closed; Telegram says
            when it's done. For "now", use plug_on / plug_off instead. Cancel with cancel_reminder("lamp").

            Args:
                on: True to turn it on, False to turn it off.
                due: Local time as "2026-10-07 21:30" (work it out from the current time).
                plug: The plug's name in the SwitchBot app, e.g. "lamp"; empty when there's only one.
            """
            now = clock()
            try:
                when = parse_due(due)
            except ValueError:
                return f"Couldn't read the time '{due}'. Use 2026-10-07 21:30."
            if not when or datetime.strptime(when, "%Y-%m-%d %H:%M") < now.replace(second=0, microsecond=0):
                return "That time has already passed. For now, use plug_on or plug_off."
            title = f"{(plug.strip() or 'plug').capitalize()} {'on' if on else 'off'}"
            action = {"tool": PLUG_TOOLS[bool(on)], "args": {"plug": plug.strip()}}
            reminder = self.add(title, when, now, action)
            return f"Scheduled: {reminder.title} at {reminder.due} (id {reminder.id})."

        @server.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
        def upcoming_reminders(limit: int = 10) -> str:
            """List Jashan's reminders, timers and scheduled plug switches that haven't gone off yet, soonest
            first."""
            items = self.open()[:limit]
            if not items:
                return "No reminders set."
            return "Reminders: " + "; ".join(f"{r.title} ({r.due or 'no time'}, id {r.id})" for r in items)

        @server.tool(annotations=change, structured_output=False)
        def cancel_reminder(which: str) -> str:
            """Cancel reminders or scheduled plug switches by id or by words in the title (e.g. "creatine",
            "lamp")."""
            hits = self.cancel(which)
            return "Cancelled: " + "; ".join(r.title for r in hits) if hits else f"No reminder matches '{which}'."

        return server
