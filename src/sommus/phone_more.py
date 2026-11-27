"""More things the iPhone can do, beyond the flashlight, a text, an alarm, music and Focus (server.Phone).

Like the rest of the phone's tools these only queue an action: /ask hands the queue back and the Shortcut runs
it. A Shortcut that sends `"v": 2` loops over the whole `actions` list; an older one does the first and nothing
else, so the server takes one action per request from it (server.Phone.queue). Each action is a small dict whose
`action` names one entry of VOCABULARY; docs/IPHONE.md has the Shortcut steps for every one.

The phone can only act while Jashan is using the Action Button: asked from Telegram, these say plainly that
nothing happened.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

# action -> the keys that come with it. The Shortcut knows exactly these and nothing else.
VOCABULARY: dict[str, tuple[str, ...]] = {
    "flashlight_on": (),
    "flashlight_off": (),
    "text": ("to", "message"),
    "alarm": ("time", "at", "label"),
    "music": ("control", "what"),
    "focus_on": ("mode",),
    "focus_off": ("mode",),
    "open_app": ("app",),
    "open_url": ("url",),
    "timer": ("seconds", "minutes", "label"),
    "volume": ("level", "fraction"),
    "brightness": ("level", "fraction"),
    "navigate": ("to", "mode", "url"),
    "shortcut": ("name", "input"),
}

MAPS_MODES = {"driving": "d", "drive": "d", "car": "d", "walking": "w", "walk": "w", "transit": "r", "bus": "r"}
BLOCKED_SCHEMES = ("javascript:", "data:", "file:")


def maps_url(to: str, mode: str = "driving") -> str:
    """Apple Maps directions from here to `to`; the Shortcut only has to open it."""
    return f"https://maps.apple.com/?daddr={quote(to)}&dirflg={MAPS_MODES.get(mode, 'd')}"


def say_span(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} second{'s' * (seconds != 1)}"
    minutes, rest = divmod(seconds, 60)
    if rest:
        return f"{minutes} min {rest} s"
    if minutes < 60 or minutes % 60:
        return f"{minutes} minute{'s' * (minutes != 1)}"
    return f"{minutes // 60} hour{'s' * (minutes != 60)}"


def register(server, queue: Callable[[dict[str, Any], str], str], quick) -> None:
    """Add the extra phone tools to the phone's MCP server. `queue(action, done)` is Phone.queue."""

    @server.tool(annotations=quick, structured_output=False)
    def phone_open(app: str = "", url: str = "") -> str:
        """Open an app or a web page/link on the iPhone. Only when the request came from the iPhone; then prefer
        this over the Mac's open_app/open_url ("open Spotify" said into the phone means the phone).

        Args:
            app: The app's name as on the home screen, e.g. "Spotify", "Maps".
            url: A link to open instead, e.g. "https://learn.uwaterloo.ca" or "spotify:".
        """
        app, url = app.strip(), url.strip()
        if url:
            if url.lower().startswith(BLOCKED_SCHEMES) or ":" not in url:
                return "That isn't a link the phone can open."
            return queue({"action": "open_url", "url": url}, "Opening that on your phone.")
        if not app:
            return "Which app?"
        return queue({"action": "open_app", "app": app}, f"Opening {app} on your phone.")

    @server.tool(annotations=quick, structured_output=False)
    def phone_timer(minutes: float = 0, seconds: int = 0, label: str = "") -> str:
        """Start a timer in the iPhone's Clock app. Only when the request came from the iPhone; then prefer this
        over set_reminder for "set a timer".

        Args:
            minutes: How long, in minutes (may be fractional).
            seconds: Extra seconds, added to `minutes`.
            label: Optional name for the timer.
        """
        total = round(float(minutes) * 60) + int(seconds)
        if not 1 <= total <= 24 * 3600:
            return "A timer has to be between 1 second and 24 hours."
        action = {"action": "timer", "seconds": total, "minutes": round(total / 60, 2), "label": label.strip()}
        return queue(action, f"Timer on your phone for {say_span(total)}.")

    def level(kind: str, value: int) -> str:
        if not 0 <= int(value) <= 100:
            return f"{kind.capitalize()} must be 0 to 100."
        value = int(value)
        return queue(
            {"action": kind, "level": value, "fraction": round(value / 100, 2)},
            f"Phone {kind} to {value}%.",
        )

    @server.tool(annotations=quick, structured_output=False)
    def phone_volume(level_percent: int) -> str:
        """Set the iPhone's volume. Only when the request came from the iPhone and he means the phone.

        Args:
            level_percent: 0 (silent) to 100.
        """
        return level("volume", level_percent)

    @server.tool(annotations=quick, structured_output=False)
    def phone_brightness(level_percent: int) -> str:
        """Set the iPhone's screen brightness. Only when the request came from the iPhone and he means the phone.

        Args:
            level_percent: 0 (darkest) to 100.
        """
        return level("brightness", level_percent)

    @server.tool(annotations=quick, structured_output=False)
    def phone_navigate(to: str, mode: str = "driving") -> str:
        """Open Apple Maps directions on the iPhone. Only when the request came from the iPhone.

        Args:
            to: Where to: an address or a place name, e.g. "E7 University of Waterloo".
            mode: "driving" (default), "walking" or "transit".
        """
        to, mode = to.strip(), mode.strip().lower()
        if not to:
            return "Where to?"
        if mode not in MAPS_MODES:
            return "mode must be driving, walking or transit."
        action = {"action": "navigate", "to": to, "mode": mode, "url": maps_url(to, mode)}
        return queue(action, f"Directions to {to} on your phone.")

    @server.tool(annotations=quick, structured_output=False)
    def phone_run_shortcut(name: str, input: str = "") -> str:
        """Run one of Jashan's own iPhone Shortcuts by name (for anything the phone has no tool for here).
        Only when the request came from the iPhone.

        Args:
            name: The Shortcut's exact name in the Shortcuts app.
            input: Optional text handed to it.
        """
        if not name.strip():
            return "Which Shortcut?"
        return queue({"action": "shortcut", "name": name.strip(), "input": input}, f"Running {name.strip()}.")
