"""Is Sommus OK? A free, spoken-friendly status report, and a watch on the Mac side.

"How's Sommus doing?" used to mean opening logs. This answers it in plain code ($0): is the server up, is the Mac
side (voice, Mac controls, Bluetooth) connected, is hard sleep on, what did today cost, what did last night's own-model
job do. The Mac side matters most: `sommus live` sat off from 2026-11-22 to 11-25 and nothing said so. The server now
notices when the Mac has been unreachable for a while and tells him once on Telegram (and again when it is back).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

STARTED = time.time()

# Questions this claims: made only of these words, and about Sommus or its status.
_ASKS = {"status", "health", "healthcheck", "diagnostics"}
_STATE = {"doing", "going", "running", "working", "ok", "okay", "alive", "up", "online", "well", "fine", "good"}
_FILLER = {
    "how", "hows", "how's", "is", "are", "you", "sommus", "the", "a", "my", "your", "it", "all", "everything", "report",
    "check", "give", "me", "tell", "show", "system", "systems", "server", "now", "right", "today", "please", "pls",
    "hey", "ok", "okay", "whats", "what", "s", "overall", "doing", "going", "running", "working", "alive", "up",
    "online", "well", "fine", "good", "status", "health", "healthcheck", "diagnostics", "full", "quick", "brief",
}  # fmt: skip


def is_question(text: str) -> bool:
    """ "how's sommus doing", "sommus status", "is everything running", "system status report"."""
    tokens = re.findall(r"[a-z]+", text.lower().replace("'", ""))
    if len(tokens) < 2 or len(tokens) > 8 or any(t not in _FILLER for t in tokens):  # a bare "status?" is not ours
        return False
    words = set(tokens)
    return bool(words & _ASKS) or ("sommus" in words and bool(words & _STATE))


@dataclass
class Presence:
    """Whether the Mac's nodes answer, and since when. One per server process."""

    up: bool | None = None
    since: datetime | None = None
    alerted: bool = False

    def observe(
        self, up: bool, now: datetime, after: timedelta = timedelta(minutes=30), hours: tuple[int, int] = (10, 23)
    ) -> str | None:
        """ "down" once, when the Mac has been gone for `after` during waking hours; "back" when it returns after
        an alert; else None. Overnight outages (a closed lid) wait until `hours` begin, so the alert is never a
        3 AM message about a sleeping Mac."""
        if up != self.up:
            self.up, self.since = up, now
            if up and self.alerted:
                self.alerted = False
                return "back"
            return None
        if not up and not self.alerted and self.since is not None:
            if now - self.since >= after and hours[0] <= now.hour < hours[1]:
                self.alerted = True
                return "down"
        return None


presence = Presence()


def _clock(moment: datetime) -> str:
    return f"{moment:%-I:%M %p}".replace(" 0", " ")


def _uptime() -> str:
    seconds = int(time.time() - STARTED)
    if seconds < 90:
        return "just started"
    if seconds < 3600 * 2:
        return f"{seconds // 60} minutes"
    if seconds < 3600 * 48:
        return f"{seconds // 3600} hours"
    return f"{seconds // 86400} days"


def last_night(data_dir: Path) -> str:
    """The last line of the nightly own-model job's log ("Night 8: 160 real examples ..."), or ''."""
    log = Path(data_dir) / "training" / "nightly.log"
    try:
        lines = [line for line in log.read_text().splitlines() if line.startswith("Night ")]
    except OSError:
        return ""
    return lines[-1] if lines else ""


def report(
    *,
    mac_nodes: list[str],
    reachable,
    asleep: bool,
    requests: int,
    fast: int,
    dollars: float,
    month_dollars: float,
    cap: float,
    data_dir: Path,
    now: datetime,
    long: bool = False,
) -> str:
    """The status, in words. `long` is the Telegram version (one fact per line); the short one is spoken."""
    mac_up = bool(mac_nodes) and all(reachable(name) for name in mac_nodes)
    if not mac_nodes:
        mac = "no Mac connected"
    elif mac_up:
        mac = "online"
    elif presence.since is not None and presence.up is False:
        mac = f"offline since {_clock(presence.since)}"
    else:
        mac = "offline"
    cents = f"{dollars * 100:.0f} cents" if dollars < 1 else f"${dollars:.2f}"
    free = f", {fast} of them free" if requests else ""
    sleep = "in hard sleep" if asleep else "awake"
    if not long:
        verdict = "All good." if mac_up or not mac_nodes else "The server is fine, but the Mac side is down."
        return (
            f"{verdict} Up {_uptime()}, {sleep}. Mac side {mac}. "
            f"Today {requests} request{'s' if requests != 1 else ''}{free}, {cents}."
        )
    lines = [
        f"Sommus server: up {_uptime()}, {sleep}",
        f"Mac side (voice, Mac controls, Bluetooth): {mac}",
        f"Today: {requests} request{'s' if requests != 1 else ''}{free}, {cents}",
        f"This month: ${month_dollars:.2f} of ${cap:.0f}",
    ]
    if night := last_night(data_dir):
        lines.append(f"Last night's training job: {night}")
    return "\n".join(lines)
