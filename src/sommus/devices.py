"""Which device does a request that several could do: plain code, no model.

"Play music" or "set a timer" can happen on the Mac, on the server or on the iPhone. The device Jashan is talking
from goes first (the iPhone for /ask, the Mac for live mode); then the Mac, the server's own tools, and the phone
last, since it can only act while he is using the Action Button. A device that isn't available right now (the Mac
asleep, the phone not in use) is passed over. When none of them is, the request keeps its first choice, so the
reply is that device's own "couldn't reach it" instead of a guess.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Iterable

from sommus.brain.fastpath import Match

DEFAULT_ORDER = ("mac", "home", "server", "phone")
LABELS = {"mac": "Mac", "phone": "Phone", "home": "Home", "server": "Sommus"}


def order(here: str = "") -> list[str]:
    """The devices to try, the one he's talking from first."""
    return ([here] if here else []) + [d for d in DEFAULT_ORDER if d != here]


def device_of(node: str | None, mac_nodes: Iterable[str]) -> str:
    """'mac', 'phone', 'home' (the plugs) or 'server' for the node that owns a tool."""
    if node == "phone":
        return "phone"
    if node in set(mac_nodes):
        return "mac"
    if node == "home":
        return "home"
    return "server"


def pick(options: dict[str, Match], here: str, available: Callable[[str], bool]) -> Match | None:
    for device in order(here):
        if device in options and available(device):
            return options[device]
    return None


TIMER_TITLE = re.compile(r"^Timer \((\d+(?:\.\d+)?) (min|hr)\)$")


def _timer_seconds(quick: Match) -> int | None:
    found = TIMER_TITLE.match(str(quick.args.get("title", "")))
    if not found:
        return None
    return round(float(found[1]) * (3600 if found[2] == "hr" else 60))


def _echo(result: str) -> str:
    return result


def options_for(quick: Match) -> dict[str, Match]:
    """The same request on each device that can do it; one entry means only one device can."""
    phone = None
    if quick.intent in ("play_pause", "next", "previous") and quick.tool == "media_control":
        wanted = str(quick.args.get("action", "play"))
        control = {"pause": "pause", "next": "next", "previous": "previous"}.get(wanted, "play")
        phone = Match(quick.intent, "phone_music", {"control": control}, phrase=_echo)
        return {"mac": quick, "phone": phone}
    if quick.intent == "open_app" and quick.tool == "open_app":
        phone = Match("open_app", "phone_open", {"app": quick.args.get("name", "")}, phrase=_echo)
        return {"mac": quick, "phone": phone}
    if quick.intent == "timer" and (seconds := _timer_seconds(quick)):
        phone = Match("timer", "phone_timer", {"seconds": seconds}, phrase=_echo)
        # set_reminder is the server's (a Telegram ping); without a server it falls back to the Mac's Reminders.
        return {"server": quick, "phone": phone}
    return {}


def route(quick: Match, here: str, available: Callable[[str], bool]) -> Match:
    """`quick` moved to the device that should do it, or unchanged."""
    options = options_for(quick)
    if len(options) < 2:
        return quick
    chosen = pick(options, here, available)
    if chosen is None or chosen is quick:
        return quick
    return dataclasses.replace(chosen, fallback=None, fallback_args=None)
