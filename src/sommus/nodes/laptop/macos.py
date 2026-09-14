"""macOS actions for the laptop node.

Plain functions with typed arguments. Nothing here builds a shell string from
model input: every command is an argument list, and AppleScript receives user
text through `argv`, never by string formatting.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from urllib.parse import urlparse


class ActionError(Exception):
    """An expected failure, safe to show the model (app not found, bad URL...)."""


def _run(args: list[str], timeout: float = 10) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ActionError(f"{args[0]} timed out") from e
    if proc.returncode != 0:
        raise ActionError(proc.stderr.strip() or f"{args[0]} exited with code {proc.returncode}")
    return proc.stdout.strip()


def _osascript(*lines: str, argv: tuple[str, ...] = ()) -> str:
    args = ["osascript"]
    for line in lines:
        args += ["-e", line]
    return _run([*args, *argv])


# ---------------------------------------------------------------- battery


def parse_battery(pmset_output: str) -> str:
    source = re.search(r"drawing from '([^']+)'", pmset_output)
    batt = re.search(r"(\d+)%; ([^;]+);\s*([^\n]*?)\s*present", pmset_output)
    if not batt:
        return "No battery information available."
    percent, state, remaining = batt.groups()
    parts = [f"{percent}%", state.strip()]
    if remaining and "no estimate" not in remaining:
        parts.append(remaining.strip())
    if source:
        parts.append(f"on {source.group(1)}")
    return ", ".join(parts)


def battery() -> str:
    return parse_battery(_run(["pmset", "-g", "batt"]))


# ---------------------------------------------------------------- volume


@dataclass(frozen=True)
class VolumeState:
    output: int | None  # None when the output device has no software volume
    muted: bool


def parse_volume(settings: str) -> VolumeState:
    out = re.search(r"output volume:(\d+|missing value)", settings)
    muted = re.search(r"output muted:(true|false)", settings)
    level = int(out.group(1)) if out and out.group(1).isdigit() else None
    return VolumeState(output=level, muted=bool(muted and muted.group(1) == "true"))


def volume() -> VolumeState:
    return parse_volume(_osascript("get volume settings"))


def set_volume(level: int) -> VolumeState:
    if not 0 <= level <= 100:
        raise ActionError("Volume must be between 0 and 100.")
    # Setting a level implies wanting to hear it.
    _osascript(f"set volume output volume {int(level)} without output muted")
    return volume()


def set_muted(muted: bool) -> VolumeState:
    _osascript(f"set volume output muted {'true' if muted else 'false'}")
    return volume()


# ---------------------------------------------------------------- apps


@dataclass(frozen=True)
class RunningApp:
    name: str
    pid: int


def parse_lsappinfo(listing: str) -> list[RunningApp]:
    """Foreground (Dock-visible) apps from `lsappinfo list`."""
    apps = []
    for block in re.split(r"\n(?=\s*\d+\) \")", listing):
        header = re.match(r'\s*\d+\) "(.+?)" ASN:', block)
        pid = re.search(r"pid = (\d+)", block)
        if header and pid and 'type="Foreground"' in block:
            apps.append(RunningApp(name=header.group(1), pid=int(pid.group(1))))
    return apps


def running_apps() -> list[RunningApp]:
    # lsappinfo, not NSWorkspace: NSWorkspace's list goes stale in a process
    # without a Cocoa run loop, which an MCP server is.
    return parse_lsappinfo(_run(["lsappinfo", "list"]))


def frontmost_app() -> str | None:
    asn = _run(["lsappinfo", "front"])
    if not asn:
        return None
    name = re.search(r'"LSDisplayName"="(.+)"', _run(["lsappinfo", "info", "-only", "name", asn]))
    return name.group(1) if name else None


def open_app(name: str) -> None:
    _run(["open", "-a", name])


def _find_app(name: str) -> RunningApp:
    wanted = name.casefold().removesuffix(".app")
    apps = running_apps()
    for app in apps:
        if app.name.casefold() == wanted:
            return app
    matches = [a for a in apps if wanted in a.name.casefold()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ActionError(f"'{name}' matches several apps: {', '.join(a.name for a in matches)}.")
    raise ActionError(f"No running app named '{name}'.")


def quit_app(name: str) -> str:
    from AppKit import NSRunningApplication

    app = _find_app(name)
    running = NSRunningApplication.runningApplicationWithProcessIdentifier_(app.pid)
    # terminate() asks the app to quit normally, so it can prompt to save work.
    if running is None or not running.terminate():
        raise ActionError(f"{app.name} did not accept the quit request.")
    return app.name


# ---------------------------------------------------------------- web


def open_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ActionError("Only http:// and https:// URLs can be opened.")
    _run(["open", url])


# ---------------------------------------------------------------- notifications


def notify(title: str, message: str) -> None:
    _osascript(
        "on run argv",
        "display notification (item 2 of argv) with title (item 1 of argv)",
        "end run",
        argv=(title, message),
    )


# ---------------------------------------------------------------- keyboard events
# Posting synthetic key events requires Accessibility permission for the app
# that launched Sommus (Terminal, iTerm, VS Code...). Without it, macOS drops
# the events silently, so check first and fail loudly.


def can_post_events() -> bool:
    import Quartz

    return bool(Quartz.CGPreflightPostEventAccess())


def _require_event_access() -> None:
    if not can_post_events():
        raise ActionError(
            "macOS blocked this: give your terminal app Accessibility permission "
            "(System Settings → Privacy & Security → Accessibility), then restart Sommus."
        )


MEDIA_KEYS = {"play_pause": 16, "next": 17, "previous": 18}  # NX_KEYTYPE_* codes


def media_key(action: str) -> None:
    import Quartz
    from AppKit import NSEvent

    if action not in MEDIA_KEYS:
        raise ActionError(f"Unknown media action '{action}'.")
    _require_event_access()
    key = MEDIA_KEYS[action]
    for state in (0xA, 0xB):  # key down, key up
        event = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), state << 8, 0, 0, None, 8, (key << 16) | (state << 8), -1
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event.CGEvent())


def lock_screen() -> None:
    import Quartz

    _require_event_access()
    q_key = 12
    flags = Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskControl
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, q_key, down)
        Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


# ---------------------------------------------------------------- power


def sleep_display() -> None:
    _run(["pmset", "displaysleepnow"])


def sleep_computer() -> None:
    _run(["pmset", "sleepnow"])
