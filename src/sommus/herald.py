"""Where something Sommus says first goes: out loud on the Mac when it's awake and it's a decent hour, else
Telegram.

brief.py writes the lines (a written one for the phone, a spoken one for the room); this picks the route. The
Mac's `announce` refuses when it's muted or in quiet mode, and a sleeping Mac doesn't answer, so every spoken
line that doesn't get said still reaches Telegram. Nothing here calls a model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, time

from sommus.brain.nodes import NodeHub

SAY_TIMEOUT = 10.0  # a Mac that just fell asleep can hold a request open for minutes: Telegram it instead


def clock_time(text: str) -> time:
    hour, minute = (int(x) for x in text.split(":"))
    return time(hour, minute)


def in_quiet_hours(moment: time, start: time, end: time) -> bool:
    """[start, end), and it may wrap past midnight (23:00 → 07:30)."""
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


class Herald:
    def __init__(
        self,
        hub: NodeHub,
        tell: Callable[[str], Awaitable[None]],
        settings: dict,
        log: Callable[[str], None] = print,
        node: str = "laptop",
    ):
        self.hub, self.tell, self.log, self.node = hub, tell, log, node
        self.enabled = bool(settings.get("speak", True))
        start, end = settings.get("quiet_hours", ["23:00", "07:30"])
        self.quiet_from, self.quiet_until = clock_time(start), clock_time(end)

    def quiet(self, now: datetime) -> bool:
        return in_quiet_hours(now.time(), self.quiet_from, self.quiet_until)

    def can_speak(self, now: datetime) -> bool:
        return self.enabled and not self.quiet(now) and self.hub.reachable(self.node)

    async def say(self, spoken: str, now: datetime) -> bool:
        """Say it on the Mac; False when it wasn't said (asleep, muted, quiet hours or quiet mode)."""
        if not spoken or not self.can_speak(now):
            return False
        try:
            result = await asyncio.wait_for(self.hub.call_on(self.node, "announce", {"text": spoken}), SAY_TIMEOUT)
        except TimeoutError:
            self.log("Announce: the Mac didn't answer in time")
            return False
        if result.is_error:
            self.log(f"Announce: {result.text[:120]}")
            return False
        self.log(f"Said on the Mac: {spoken}")
        return True

    async def deliver(self, text: str, spoken: str, now: datetime, telegram_too: bool = False) -> bool:
        """Speak `spoken` if it can; Telegram gets `text` when it couldn't (or always, with telegram_too).
        Returns whether it was said."""
        said = await self.say(spoken, now)
        if text and (telegram_too or not said):
            await self.tell(text)
        return said
