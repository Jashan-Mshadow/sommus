"""macOS actions for the laptop node.

Plain functions with typed arguments. Nothing here builds a shell string from
model input: every command is an argument list, and AppleScript receives user
text through `argv`, never by string formatting.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
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


def _osascript(*lines: str, argv: tuple[str, ...] = (), timeout: float = 10) -> str:
    args = ["osascript"]
    for line in lines:
        args += ["-e", line]
    return _run([*args, *argv], timeout=timeout)


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


def protected_apps() -> set[str]:
    """Apps Sommus must not quit — it runs inside one of them."""
    return {a.strip().casefold() for a in os.environ.get("SOMMUS_PROTECT_APPS", "").split(",") if a.strip()}


def quit_app(name: str) -> str:
    from AppKit import NSRunningApplication

    app = _find_app(name)
    if app.name.casefold() in protected_apps():
        raise ActionError(f"{app.name} is protected — Sommus runs inside it, so quitting it would kill Sommus.")
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


# ---------------------------------------------------------------- brightness
# macOS ships no brightness command. This is Apple's private DisplayServices
# framework — the same call the brightness keys use. Built-in display only.


def _display_services():
    import ctypes

    try:
        lib = ctypes.CDLL("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices")
    except OSError as e:
        raise ActionError("This Mac doesn't expose brightness control.") from e
    lib.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_float)]
    lib.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
    return lib, ctypes


MAIN_DISPLAY = 1


def brightness() -> int:
    lib, ctypes = _display_services()
    level = ctypes.c_float()
    if lib.DisplayServicesGetBrightness(MAIN_DISPLAY, ctypes.byref(level)) != 0:
        raise ActionError("Couldn't read the display brightness.")
    return round(level.value * 100)


def set_brightness(level: int) -> int:
    if not 0 <= level <= 100:
        raise ActionError("Brightness must be between 0 and 100.")
    lib, _ = _display_services()
    if lib.DisplayServicesSetBrightness(MAIN_DISPLAY, level / 100) != 0:
        raise ActionError("Couldn't set the display brightness (external displays aren't supported).")
    return level


# ---------------------------------------------------------------- wi-fi


def wifi_status() -> tuple[bool, str | None]:
    on = "On" in _run(["networksetup", "-getairportpower", "en0"])
    ssid = None
    if on:
        summary = _run(["ipconfig", "getsummary", "en0"])
        match = re.search(r"^\s*SSID\s*:\s*(.+)$", summary, re.MULTILINE)
        ssid = match.group(1).strip() if match else None
    return on, ssid


def set_wifi(on: bool) -> tuple[bool, str | None]:
    _run(["networksetup", "-setairportpower", "en0", "on" if on else "off"])
    return wifi_status()


# ---------------------------------------------------------------- keyboard shortcuts

KEY_CODES = {
    "a": 0,
    "b": 11,
    "c": 8,
    "d": 2,
    "e": 14,
    "f": 3,
    "g": 5,
    "h": 4,
    "i": 34,
    "j": 38,
    "k": 40,
    "l": 37,
    "m": 46,
    "n": 45,
    "o": 31,
    "p": 35,
    "q": 12,
    "r": 15,
    "s": 1,
    "t": 17,
    "u": 32,
    "v": 9,
    "w": 13,
    "x": 7,
    "y": 16,
    "z": 6,
    "0": 29,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "5": 23,
    "6": 22,
    "7": 26,
    "8": 28,
    "9": 25,
    "space": 49,
    "return": 36,
    "enter": 36,
    "tab": 48,
    "delete": 51,
    "escape": 53,
    "esc": 53,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "comma": 43,
    "period": 47,
    "slash": 44,
    "grave": 50,
    "minus": 27,
    "equal": 24,
    "brightnessup": 144,
    "brightnessdown": 145,
    "f11": 103,
    "f12": 111,
}


def press_keys(combo: str) -> str:
    """'cmd+s', 'cmd+shift+t', 'escape' — a real keystroke to the focused app."""
    import Quartz

    _require_event_access()
    parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]
    modifiers = {
        "cmd": Quartz.kCGEventFlagMaskCommand,
        "command": Quartz.kCGEventFlagMaskCommand,
        "shift": Quartz.kCGEventFlagMaskShift,
        "alt": Quartz.kCGEventFlagMaskAlternate,
        "option": Quartz.kCGEventFlagMaskAlternate,
        "ctrl": Quartz.kCGEventFlagMaskControl,
        "control": Quartz.kCGEventFlagMaskControl,
        "fn": Quartz.kCGEventFlagMaskSecondaryFn,
    }
    flags = 0
    key = None
    for part in parts:
        if part in modifiers:
            flags |= modifiers[part]
        elif part in KEY_CODES:
            key = KEY_CODES[part]
        else:
            raise ActionError(f"Unknown key '{part}' in '{combo}'.")
    if key is None:
        raise ActionError(f"No main key in '{combo}' — try something like 'cmd+s'.")
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, key, down)
        Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    return combo


def type_text(text: str, press_return: bool = False) -> int:
    """Type a literal string into the focused app, unicode included."""
    import Quartz

    _require_event_access()
    if not text:
        raise ActionError("Nothing to type.")
    # CGEventKeyboardSetUnicodeString bypasses key codes, so any character works.
    for chunk in (text[i : i + 20] for i in range(0, len(text), 20)):
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(event, len(chunk), chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.012)  # let the target app keep up
    if press_return:
        press_keys("return")
    return len(text)


# ---------------------------------------------------------------- screen


_last_shot: tuple[int, int] | None = None  # size of the most recent screenshot, for click mapping


def screen_size() -> tuple[int, int]:
    import Quartz

    display = Quartz.CGMainDisplayID()
    return Quartz.CGDisplayPixelsWide(display), Quartz.CGDisplayPixelsHigh(display)


def screenshot(max_width: int = 1200) -> tuple[Path, int, int]:
    """Capture the screen to a temp JPEG, downscaled so it costs few tokens."""
    path = Path(tempfile.gettempdir()) / "sommus-screen.jpg"
    try:
        _run(["screencapture", "-x", "-t", "jpg", str(path)], timeout=20)
    except ActionError as e:
        raise ActionError(
            "macOS blocked the screenshot: give your terminal app Screen Recording permission "
            "(System Settings → Privacy & Security → Screen Recording), then restart Sommus."
        ) from e
    if not path.exists() or path.stat().st_size == 0:
        raise ActionError(
            "The screenshot came back empty — Screen Recording permission is probably missing "
            "(System Settings → Privacy & Security → Screen Recording)."
        )
    _run(["sips", "-Z", str(max_width), "-s", "formatOptions", "70", str(path)], timeout=20)
    dimensions = _run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)], timeout=20)
    width = int(re.search(r"pixelWidth: (\d+)", dimensions).group(1))
    height = int(re.search(r"pixelHeight: (\d+)", dimensions).group(1))
    global _last_shot
    _last_shot = (width, height)
    return path, width, height


def click(x: int, y: int, double: bool = False) -> tuple[int, int]:
    """Click at coordinates from the most recent screenshot (scaled back to screen points)."""
    import Quartz

    _require_event_access()
    screen_w, screen_h = screen_size()
    if _last_shot:
        scale = screen_w / _last_shot[0]
        x, y = round(x * scale), round(y * scale)
    if not (0 <= x <= screen_w and 0 <= y <= screen_h):
        raise ActionError(f"({x}, {y}) is off screen — the screen is {screen_w}x{screen_h} points.")
    for event_type in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        event = Quartz.CGEventCreateMouseEvent(None, event_type, (x, y), Quartz.kCGMouseButtonLeft)
        if double and event_type != Quartz.kCGEventMouseMoved:
            Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, 2)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.02)
    if double:
        for event_type in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            event = Quartz.CGEventCreateMouseEvent(None, event_type, (x, y), Quartz.kCGMouseButtonLeft)
            Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, 2)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            time.sleep(0.02)
    return x, y


# ---------------------------------------------------------------- shell

BLOCKED_COMMANDS = ("sudo", "su ")  # would sit waiting for a password nobody can type


def run_shell(command: str, timeout: float = 60) -> tuple[str, int]:
    """Run a shell command and return its combined output and exit code."""
    if any(command.strip().startswith(blocked) for blocked in BLOCKED_COMMANDS):
        raise ActionError("Commands needing sudo can't run here — they'd wait forever for a password.")
    try:
        proc = subprocess.run(
            ["/bin/zsh", "-lc", command], capture_output=True, text=True, timeout=timeout, cwd=Path.home()
        )
    except subprocess.TimeoutExpired as e:
        raise ActionError(f"'{command}' was still running after {timeout:g}s and was stopped.") from e
    output = (proc.stdout + proc.stderr).strip()
    return output, proc.returncode


# ---------------------------------------------------------------- power


def sleep_display() -> None:
    _run(["pmset", "displaysleepnow"])


def sleep_computer() -> None:
    _run(["pmset", "sleepnow"])
