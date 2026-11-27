"""Wake-ups: "wake me up at 7:50", "at 7:50 say time for the gym out loud", said on the Mac at that time.

`announce` only speaks right now, so a wake-up is kept the way a plug timer is: a reminder on the server
(reminders.py) whose `action` is {"tool": "wakeup", ...}. At its time the scheduler hands it here instead of
pinging, and this rings it:

- **Telegram, every ring**, with an "I'm up" button: the phone always gets it, whatever the Mac is doing.
- **Out loud on the Mac** (`announce`), even in quiet hours (he asked for it, that's the point), but never during
  a class or calendar event unless the sound is in his headphones (`Server.busy`, the nudges' rule). Before the
  first line it makes sure he can hear it: volume up to `[wakeup] volume` if lower, unmuted, quiet mode lifted.
  All three are put back when the alarm ends.
- **Repeats** every `repeat_minutes`, up to `rings` times, until he answers: any message to Sommus (Telegram, the
  iPhone, the Mac) or the button stops it. "I'm up" / "okay" / "stop" alone gets a free "Morning." back.
- A Mac that is asleep or unreachable gets nothing; Telegram still does, and the activity log says why.

The phone itself can't be reached at alarm time: the iPhone only acts inside an Action Button request. When the
wake-up is set from the phone and its Shortcut has shown it can make alarms, a Clock alarm is handed over too
(server.Phone.backup_alarm). Everything here is plain code: $0, no model.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sommus.brain import fastpath

TOOL = "wakeup"  # the reminder action's "tool": not a real tool, the scheduler hands these to WakeUps
NODE = "laptop"
CALL_TIMEOUT = 10.0  # a Mac that just fell asleep can hold a call open for minutes
DEFAULTS = {"volume": 50, "repeat_minutes": 2, "rings": 3, "restore_after_seconds": 60}
UP_BUTTON = [[("✅ I'm up", "wake:up")]]


def owns(reminder) -> bool:
    return bool(reminder.action) and reminder.action.get("tool") == TOOL


# ---------------------------------------------------------------- reading the request ($0)
# Whole-sentence templates, like the reminder ones in fastpath.py: a sentence that doesn't fit goes to the model.
_LEAD = fastpath._LEAD
_COUNT = fastpath._COUNT
_UNIT = fastpath._UNIT
_IN = rf"in\s+(?P<n>{_COUNT})\s+(?P<unit>{_UNIT})"
_CLOCK = (
    r"(?:(?P<day>today|tonight|tomorrow)\s+)?(?:(?:at|for)\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*"
    r"(?P<ap>a\.?m\.?|p\.?m\.?)?(?:\s+(?P<day2>today|tonight|tomorrow)(?:\s+morning)?)?"
)
_WHEN = rf"(?:{_IN}|{_CLOCK})"
_END = r"(?:\s+please)?[.!?\s]*$"
_FOR = r"(?:\s+(?:for|to)\s+(?P<why>[a-z' ]{2,40}?))?"  # "wake me up at 7 for the gym"
_WAKE = re.compile(_LEAD + rf"wake\s+me(?:\s+up)?\s+{_WHEN}{_FOR}{_END}", re.I)
_ALARM = re.compile(
    _LEAD + r"(?:set|make|create|schedule)\s+(?:me\s+)?(?:a|an|my)\s+"
    r"(?:spoken|voice|talking|loud|out\s*loud|wake[\s-]*up)\s+(?:alarm|call)?\s*" + _WHEN + _FOR + _END,
    re.I,
)
_LOUD = r"(?:out\s+loud|aloud|outloud)"
_SAY = r"(?:say|speak)"
_SAY_LATER = [
    re.compile(_LEAD + rf"(?:{_WHEN}),?\s+{_SAY}\s+{_LOUD}\s+(?P<msg>.+?){_END}", re.I),
    re.compile(_LEAD + rf"(?:{_WHEN}),?\s+{_SAY}\s+(?P<msg>.+?)\s+{_LOUD}{_END}", re.I),
    re.compile(_LEAD + rf"(?:{_WHEN}),?\s+announce\s+(?P<msg>.+?){_END}", re.I),
    re.compile(_LEAD + rf"{_SAY}\s+(?P<msg>.+?)\s+{_LOUD}\s+{_WHEN}{_END}", re.I),
    re.compile(_LEAD + rf"announce\s+(?P<msg>.+?)\s+{_WHEN}{_END}", re.I),
]
_CANCEL = re.compile(
    _LEAD + r"(?:cancel|delete|remove|clear|disable|turn\s+off|switch\s+off|get\s+rid\s+of)\s+"
    r"(?:(?:the|my|all(?:\s+(?:of\s+)?my)?|that)\s+)?(?:spoken\s+|wake[\s-]*up\s+)?"
    r"(?:alarms?|wake[\s-]*ups?(?:\s+calls?)?|wake[\s-]*up\s+alarms?)"
    r"(?:\s+(?:for\s+)?(?:tomorrow|tonight|today)(?:\s+morning)?)?" + _END,
    re.I,
)
_TOO_VAGUE = {"me", "it", "this", "that", "something"}
_STOP = {"and", "then", "also", "if", "when", "unless", "every", "each", "daily", "weekly"}
# What he says to stop a ringing alarm. Only read while one rings, and only as a whole message.
ACK_WORDS = {
    "i", "m", "im", "am", "up", "awake", "stop", "ok", "okay", "alright", "all", "right", "got", "it", "thanks",
    "thank", "you", "sommus", "yes", "yeah", "yep", "good", "morning", "turn", "off", "the", "alarm", "wake",
    "wakeup", "cancel", "enough", "shut", "quiet", "be", "please", "fine", "cool", "now", "already", "my", "that",
    "hey", "sure", "on", "its", "out", "of", "bed", "getting", "dismiss",
}  # fmt: skip


def _due(m: re.Match[str], now: datetime) -> datetime | None:
    """The time asked for, by the plug timers' rule: with no AM/PM, whichever of the two comes next ("at 7:50"
    at 11 PM is 7:50 AM). "tomorrow at 7" is the morning (7 AM; "tomorrow at 12" is noon), "tonight" is PM."""
    g = m.groupdict()
    if g.get("n"):
        minutes = fastpath._minutes(g["n"], g["unit"])
        if not minutes or minutes > 24 * 60:
            return None
        return (now + timedelta(minutes=minutes, seconds=30)).replace(second=0, microsecond=0)
    if not g.get("h"):
        return None
    hour, minute, ap = int(g["h"]), int(g.get("m") or 0), (g.get("ap") or "").lower()
    day = g.get("day") or g.get("day2")
    if minute > 59 or hour > 23 or (ap and not 1 <= hour <= 12):
        return None
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if ap:
        hours = [hour % 12 + (12 if ap.startswith("p") else 0)]
    elif hour >= 13 or hour == 0:
        hours = [hour]
    elif day == "tonight":
        hours = [hour % 12 + 12]
    elif day == "tomorrow":
        hours = [hour]  # 7 is 7 AM, 12 is noon: the first time the clock shows it tomorrow, past midnight's 12
    else:
        hours = [hour % 12, hour % 12 + 12]
    days = [1] if day == "tomorrow" else [0] if day in ("today", "tonight") else [0, 1]
    times = sorted(base + timedelta(days=d, hours=h, minutes=minute) for d in days for h in hours)
    when = next((t for t in times if t > now), None)
    if when is None or when - now > timedelta(days=2):
        return None
    return when


def _clean(text: str) -> str:
    text = " ".join(text.strip(" ,.!?\"'").split())
    return text[:1].upper() + text[1:] if text else ""


def say_time(when: datetime, now: datetime) -> str:
    """'7:50 AM', '7 AM tomorrow', '8:10 PM on Friday'."""
    clock = f"{when:%-I:%M %p}".replace(":00 ", " ")
    if when.date() == now.date():
        return clock
    if when.date() == now.date() + timedelta(days=1):
        return f"{clock} tomorrow"
    return f"{clock} on {when:%A}"


def match(text: str, now: datetime | None = None) -> fastpath.Match | None:
    """The common phrasings, free: set ("wake me up at 7:50"), say later ("at 7:50 say time for the gym out
    loud"), cancel ("cancel my alarm"). Anything else is the model's (schedule_wakeup / cancel_wakeup)."""
    now = now or datetime.now()
    raw = " ".join(text.strip().split())
    if _CANCEL.match(raw):
        return fastpath.Match("wakeup", "cancel_wakeup", {}, phrase=lambda result: result)
    plain, found = False, _WAKE.match(raw) or _ALARM.match(raw)
    if not found:
        plain, found = True, next((m for p in _SAY_LATER if (m := p.match(raw))), None)
    if not found:
        return None
    due = _due(found, now)
    if due is None:
        return None
    g = found.groupdict()
    say = _clean(g.get("msg") or "")
    why = _clean(g.get("why") or "")
    if plain:
        tokens = set(fastpath.words(say))
        if not say or len(say) > 160 or tokens <= _TOO_VAGUE or re.search(rf"\b{_LOUD}\b", say, re.I):
            return None
    elif why:
        tokens = set(fastpath.words(why))
        if tokens & _STOP or tokens & {"at", "in", "by", "tomorrow", "tonight", "today"}:
            return None
        joined = "to" if re.search(r"\bto\s+" + re.escape(g["why"].strip()), raw, re.I) else "for"
        say = f"Time {joined} {why[0].lower()}{why[1:]}."  # "for the gym" / "to go to the gym"
    args: dict[str, Any] = {"due": f"{due:%Y-%m-%d %H:%M}", "say": say, "once": plain}
    if plain:
        said = f'I\'ll say "{say}" out loud at {say_time(due, now)}.'
    elif g.get("n"):
        said = f"Wake-up set for {say_time(due, now)}, in {fastpath._say_span(due - now)}."
    else:
        said = f"Wake-up set for {say_time(due, now)}."

    def phrase(result: str) -> str:
        if not result.startswith(("Wake-up set", "Will say")):
            return result  # refused (a time gone by): say why, never claim it's set
        extra = next((line for line in result.splitlines()[1:] if line.startswith("Your phone")), "")
        return f"{said} {extra}".strip()

    return fastpath.Match("wakeup", "schedule_wakeup", args, phrase=phrase)


def is_ack(text: str) -> bool:
    """'I'm up', 'okay', 'stop', 'turn off the alarm': nothing but a way of saying he's awake."""
    tokens = fastpath.words(text)
    return bool(tokens) and len(tokens) <= 8 and all(t in ACK_WORDS for t in tokens)


# ---------------------------------------------------------------- the server side


class WakeUps:
    """Keeps and rings wake-ups. Built in `sommus serve` before the Server; the Server then fills in how to
    reach Telegram, the class/event check and the activity log (attach)."""

    def __init__(
        self,
        reminders,
        hub,
        data_dir: Path,
        settings: dict | None = None,
        user: str = "Jashan",
        clock: Callable[[], datetime] = datetime.now,
        phone_alarm: Callable[[datetime], str] | None = None,
    ):
        self.reminders, self.hub, self.user, self.clock = reminders, hub, user, clock
        self.settings = DEFAULTS | (settings or {})
        self.path = data_dir / "wakeup.json"
        self.phone_alarm = phone_alarm  # from the iPhone: also hand its Clock an alarm, when it can make one
        self.tell: Callable[..., Awaitable[None]] | None = None
        self.busy: Callable[[datetime], Awaitable[str]] | None = None
        self.private_audio: Callable[[], Awaitable[bool]] | None = None
        self.record: Callable[[str, str, str], None] | None = None
        self.log: Callable[[str], None] = print
        self.active: dict[str, Any] | None = None  # the one ringing now (and what to put back after)
        with contextlib.suppress(OSError, ValueError):
            self.active = json.loads(self.path.read_text()) or None

    def attach(self, tell, busy=None, private_audio=None, record=None, log=None) -> None:
        self.tell, self.busy, self.private_audio, self.record = tell, busy, private_audio, record
        if log:
            self.log = log

    def _save(self) -> None:
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.active))

    def ringing(self) -> bool:
        """An alarm is going and waits for his answer ("say X out loud at 7" is said once: nothing to answer)."""
        return self.active is not None and not self.active.get("stopped") and not self.active.get("once")

    def pending(self) -> list:
        return [r for r in self.reminders.open() if owns(r)]

    # -- the tools

    def add(self, due: str, say: str = "", once: bool = False, now: datetime | None = None) -> str:
        from sommus.reminders import parse_due

        now = now or self.clock()
        try:
            when = parse_due(due)
        except ValueError:
            return f"Couldn't read the time '{due}'. Use 2026-11-27 07:50."
        if not when:
            return "When? Give a time, like 2026-11-27 07:50."
        at = datetime.strptime(when, "%Y-%m-%d %H:%M")
        if at <= now.replace(second=0, microsecond=0):
            return "That time has already passed. For right now, use announce."
        if at - now > timedelta(days=7):
            return "That's more than a week away; set a reminder (set_reminder) instead."
        say = " ".join(say.split())[:300]
        rings = 1 if once else int(self.settings["rings"])
        title = f"Say out loud: {say}" if once else f"Wake-up: {say}" if say else "Wake-up"
        reminder = self.reminders.add(
            title, when, now, {"tool": TOOL, "args": {"say": say, "once": once, "rings": rings}}
        )
        head = f'Will say "{say}" at' if once else "Wake-up set for"
        lines = [f"{head} {when} (id {reminder.id}): out loud on the Mac if it is awake (even in quiet hours, not "
                 "in a class), and on Telegram."]  # fmt: skip
        if not once and self.phone_alarm and at - now <= timedelta(hours=24):
            with contextlib.suppress(Exception):
                if extra := self.phone_alarm(at):
                    lines.append(extra)
        return "\n".join(lines)

    def node(self):
        from mcp.server.mcpserver import MCPServer
        from mcp.types import ToolAnnotations

        server = MCPServer("wakeup", log_level="WARNING")
        change = ToolAnnotations(read_only_hint=False, destructive_hint=False)

        @server.tool(annotations=change, structured_output=False)
        def schedule_wakeup(due: str, say: str = "", once: bool = False) -> str:
            """Wake Jashan up, or say something out loud, LATER: "wake me up at 7:50", "wake me up in 20
            minutes", "at 7:50 say time for the gym out loud". At the time it is said on the Mac (volume raised
            and unmuted for it, even in quiet hours, never during a class) and sent to Telegram; a wake-up
            repeats every 2 minutes, 3 times, until he answers. Runs on the server: works with the laptop
            closed. This is the way to set an alarm from Telegram or the Mac (phone_alarm only works from the
            iPhone). Cancel with cancel_wakeup.

            Args:
                due: Local time as "2026-11-27 07:50" (work it out from the current time).
                say: What to say instead of the usual "time to get up", e.g. "Time for the gym."; may be empty.
                once: True to say `say` one time only, not ring like an alarm ("say X out loud at 7").
            """
            return self.add(due, say, once)

        @server.tool(annotations=change, structured_output=False)
        async def cancel_wakeup(which: str = "") -> str:
            """Cancel wake-ups ("cancel my alarm", "cancel the wake-up"), and stop one that is ringing. Doesn't
            touch alarms in the iPhone's Clock app (nothing here can).

            Args:
                which: An id or a time like "07:50" to cancel just that one; empty cancels them all.
            """
            return await self.cancel(which)

        return server

    async def cancel(self, which: str = "") -> str:
        which = which.strip().lower()
        stopped = ""
        if self.ringing() and not which:
            stopped = await self.stop("cancelled")
        hits = [r for r in self.pending() if not which or r.id == which or which in (r.due or "")]
        for r in hits:
            r.done = True
        if hits:
            self.reminders.save()
        if not hits:
            if stopped:
                return "Alarm off."
            return "No wake-up is set." + (
                " (Alarms in your phone's Clock app are yours to remove.)" if not which else ""
            )
        times = ", ".join(say_time(r.when(), self.clock()) for r in hits)
        return f"{'Alarm off, and c' if stopped else 'C'}ancelled the wake-up{'s' * (len(hits) > 1)} for {times}."

    # -- ringing

    async def _call(self, tool: str, args: dict) -> Any:
        try:
            return await asyncio.wait_for(self.hub.call_on(NODE, tool, args), CALL_TIMEOUT)
        except TimeoutError:
            return None

    async def start(self, reminder, late: bool, now: datetime) -> None:
        """Its time has come (from the scheduler): ring it now, then keep ringing in tick()."""
        args = dict(reminder.action.get("args") or {})
        restore = {}
        if self.active:  # another one still ringing: it ends here, and what it changed is put back at the end
            restore = self.active.get("restore") or {}
        self.active = {
            "id": reminder.id,
            "due": reminder.due,
            "say": str(args.get("say") or ""),
            "once": bool(args.get("once")),
            "rings": max(1, int(args.get("rings") or self.settings["rings"])),
            "rung": 0,
            "late": late,
            "next": now.isoformat(timespec="seconds"),
            "restore": restore,
            "spoken": 0,
        }
        await self.tick(now)

    async def tick(self, now: datetime) -> None:
        """Every scheduler tick: the next ring when it's due, and the end (put the sound back) after the last."""
        a = self.active
        if not a:
            return
        if a.get("stopped") or (a["rung"] >= a["rings"] and now >= datetime.fromisoformat(a["next"])):
            await self._finish(a.get("stopped") or f"no answer after {a['rung']} ring{'s' * (a['rung'] != 1)}")
            return
        if now < datetime.fromisoformat(a["next"]):
            return
        a["rung"] += 1
        last = a["rung"] >= a["rings"]
        wait = int(self.settings["restore_after_seconds"]) if last else 60 * int(self.settings["repeat_minutes"])
        a["next"] = (now + timedelta(seconds=wait)).isoformat(timespec="seconds")
        self._save()
        written, spoken = self.lines(a, now)
        said, why = await self._speak(a, spoken, now)
        if self.tell:
            await (self.tell(written) if a["once"] else self.tell(written, UP_BUTTON))
        route = "spoken+telegram" if said else f"telegram ({why})"
        kind = "said later" if a["once"] else f"wake-up {a['rung']}/{a['rings']}"
        if self.record:
            self.record(kind, spoken if said else written, route)
        self.log(f"Wake-up {a['id']} ring {a['rung']}/{a['rings']}: {route}")
        self._save()

    def lines(self, a: dict, now: datetime) -> tuple[str, str]:
        """(Telegram text, spoken line) for this ring."""
        clock = f"{now:%-I:%M %p}"
        late = ""
        if a.get("late") and a["rung"] == 1 and a.get("due"):
            late = f" (meant for {datetime.strptime(a['due'], '%Y-%m-%d %H:%M'):%-I:%M %p}; I was late)"
        say = a["say"]
        if a["once"]:
            return f"🔊 {say}{late}", say
        then = say or "Time to get up."
        if a["rung"] == 1:
            hello = f"Good morning, {self.user}." if now.hour < 12 else f"{self.user},"
            return f"⏰ Wake up! It's {clock}.{' ' + say if say else ''}{late}", f"{hello} It's {clock}. {then}"
        return f"⏰ Still time to get up: {clock} ({a['rung']} of {a['rings']}).", f"{self.user}, it's {clock}. {then}"

    async def _speak(self, a: dict, spoken: str, now: datetime) -> tuple[bool, str]:
        """Say it on the Mac, loud enough to hear. (said, why not)."""
        if not self.hub.reachable(NODE):
            self.log("Wake-up: the Mac is asleep or unreachable; Telegram only")
            return False, "Mac unreachable"
        if self.busy:
            with contextlib.suppress(Exception):
                if (doing := await self.busy(now)) and not (self.private_audio and await self.private_audio()):
                    return False, f"busy: {doing}"
        await self._make_audible(a)
        result = await self._call("announce", {"text": spoken})
        if result is not None and result.is_error and "quiet mode" in result.text.lower():
            lifted = await self._call("set_quiet", {"on": False})
            if lifted is not None and not lifted.is_error:
                a.setdefault("restore", {})["quiet"] = True
                self._save()
                result = await self._call("announce", {"text": spoken})
        if result is None:
            return False, "Mac didn't answer"
        if result.is_error:
            return False, "not said: " + result.text.removeprefix("Didn't say it: ").rstrip(".")[:80]
        a["spoken"] = a.get("spoken", 0) + 1
        return True, ""

    async def _make_audible(self, a: dict) -> None:
        """Volume up to [wakeup] volume and unmuted, once per alarm; what it was is kept to put back."""
        restore = a.setdefault("restore", {})
        if "volume" in restore or a.get("checked_volume"):
            return
        a["checked_volume"] = True
        state = await self._call("get_volume", {})
        if state is None or state.is_error:
            return
        level, muted = fastpath.level_from(state.text), "(muted)" in state.text
        target = int(self.settings["volume"])
        if level is None or (level >= target and not muted):
            return  # loud enough already, or an output with no software volume
        changed = await self._call("set_volume", {"level": max(level, target)})  # set_volume also unmutes
        if changed is not None and not changed.is_error:
            restore.update(volume=level, muted=muted)
            self._save()

    async def _finish(self, how: str) -> None:
        a, self.active = self.active, None
        self._save()
        if not a:
            return
        restore = a.get("restore") or {}
        if restore and self.hub.reachable(NODE):
            if "volume" in restore:
                await self._call("set_volume", {"level": int(restore["volume"])})
                if restore.get("muted"):
                    await self._call("set_mute", {"muted": True})
            if restore.get("quiet"):
                await self._call("set_quiet", {"on": True})
        elif restore:
            self.log("Wake-up: couldn't put the Mac's sound back (it's unreachable)")
        if not a["once"]:
            if self.record:
                self.record("wake-up end", f"Wake-up for {a.get('due', '?')[11:]}: {how}", "-")
            self.log(f"Wake-up {a['id']} ended: {how}")

    async def stop(self, how: str) -> str:
        """He answered (or cancelled): no more rings, and the sound goes back now."""
        if not self.ringing():
            return ""
        assert self.active is not None
        self.active["stopped"] = how
        await self._finish(how)
        return "Morning. Alarm off." if self.clock().hour < 12 else "Alarm off."

    async def heard(self, text: str, source: str = "") -> str:
        """A message from him while an alarm rings: he's up, so it stops. Returns the reply when the message was
        nothing but "I'm up" (the turn ends there, free); '' when it is a real request, which goes on as usual."""
        if not self.ringing():
            return ""
        reply = await self.stop(f"answered{' on ' + source if source else ''}")
        return reply if is_ack(text) else ""
