"""Laptop node: exposes macOS actions as MCP tools.

Run standalone with `python -m sommus.nodes.laptop.server` (stdio), which is
also how Claude Desktop or Claude Code can use it directly.

Each tool's annotations declare its permission tier; the brain enforces it:
  READ         read_only_hint=True                   → runs immediately
  REVERSIBLE   destructive_hint=False                → runs, reports back
  DESTRUCTIVE  destructive_hint=True                 → asks the user first
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from mcp.types import ToolAnnotations

from sommus.nodes.laptop import apps, browser, files, macos

READ = ToolAnnotations(read_only_hint=True)
REVERSIBLE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

server = MCPServer(
    "sommus-laptop",
    instructions="Controls Jashan's MacBook: volume, apps, media, notifications, lock and sleep.",
    log_level="WARNING",
)


def tool(annotations: ToolAnnotations) -> Callable:
    """Register a tool; expected failures reach the model as a readable error."""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except macos.ActionError as e:
                raise ToolError(str(e)) from e

        server.tool(annotations=annotations, structured_output=False)(wrapper)
        return fn

    return decorator


def _describe_volume(state: macos.VolumeState) -> str:
    if state.output is None:
        return "The current output device doesn't support software volume control."
    return f"Volume {state.output}%{' (muted)' if state.muted else ''}."


@tool(READ)
def get_battery() -> str:
    """Get the MacBook's battery percentage, charging state, and time remaining."""
    return macos.battery()


@tool(READ)
def get_volume() -> str:
    """Get the Mac's output volume (0-100) and whether sound is muted — also answers "is it loud"."""
    return _describe_volume(macos.volume())


@tool(REVERSIBLE)
def set_volume(level: int) -> str:
    """Set the output volume and unmute.

    Args:
        level: Volume from 0 (silent) to 100 (maximum).
    """
    return _describe_volume(macos.set_volume(level))


@tool(REVERSIBLE)
def set_mute(muted: bool) -> str:
    """Mute or unmute sound output without changing the volume level.

    Args:
        muted: True to mute, False to unmute.
    """
    return _describe_volume(macos.set_muted(muted))


@tool(READ)
def list_apps() -> str:
    """List the apps currently open (the ones shown in the Dock), which one is in front, and which can't be quit."""
    protected = macos.protected_apps()
    names = [
        f"{a.name} (protected, can't be quit)" if a.name.casefold() in protected else a.name
        for a in sorted(macos.running_apps(), key=lambda a: a.name)
    ]
    front = macos.frontmost_app()
    return f"Open: {', '.join(names)}. In front: {front or 'unknown'}."


@tool(REVERSIBLE)
def open_app(name: str) -> str:
    """Open an app, or bring it to the front if it's already running.

    Args:
        name: The app's name as it appears in /Applications, e.g. "Spotify", "Google Chrome", "Notes".
    """
    macos.open_app(name)
    return f"Opened {name}."


@tool(DESTRUCTIVE)
def quit_app(name: str) -> str:
    """Quit a running app. The app may still ask to save unsaved work.

    Args:
        name: The app's name, e.g. "Spotify". Use list_apps first if unsure of the exact name.
    """
    return f"Asked {macos.quit_app(name)} to quit."


@tool(REVERSIBLE)
def open_url(url: str) -> str:
    """Open a web page in the default browser.

    Args:
        url: A full http:// or https:// URL.
    """
    macos.open_url(url)
    return f"Opened {url}."


@tool(REVERSIBLE)
def media_control(action: Literal["play_pause", "next", "previous"]) -> str:
    """Press a media key: toggles or skips whatever is currently playing (Spotify, Music, YouTube in a browser...).

    Args:
        action: "play_pause" toggles playback; "next" and "previous" skip tracks.
    """
    macos.media_key(action)
    return f"Pressed {action.replace('_', '/')}."


@tool(REVERSIBLE)
def notify(title: str, message: str) -> str:
    """Show a macOS notification banner right now. For a reminder at a later time, use create_reminder.

    Args:
        title: Short bold heading.
        message: Body text.
    """
    macos.notify(title, message)
    return "Notification shown."


@tool(REVERSIBLE)
def lock_screen() -> str:
    """Lock the screen immediately. Apps keep running; unlocking needs the password or Touch ID."""
    macos.lock_screen()
    return "Screen locked."


@tool(REVERSIBLE)
def sleep_display() -> str:
    """Turn the display off. Apps and downloads keep running."""
    macos.sleep_display()
    return "Display is off."


@tool(DESTRUCTIVE)
def sleep_computer() -> str:
    """Put the whole MacBook to sleep. Sommus stops responding until the lid is opened or a key is pressed."""
    macos.sleep_computer()
    return "Going to sleep."


# ---------------------------------------------------------------- display, wi-fi, keys


@tool(READ)
def get_brightness() -> str:
    """Get the built-in display's brightness as a percentage."""
    return f"Brightness {macos.brightness()}%."


@tool(REVERSIBLE)
def set_brightness(level: int) -> str:
    """Set the built-in display's brightness (external monitors aren't supported).

    Args:
        level: Brightness from 0 (darkest) to 100 (brightest).
    """
    return f"Brightness {macos.set_brightness(level)}%."


@tool(READ)
def get_wifi() -> str:
    """Check whether Wi-Fi is on and which network the Mac is joined to."""
    on, ssid = macos.wifi_status()
    if not on:
        return "Wi-Fi is off."
    return f"Wi-Fi is on, connected to {ssid}." if ssid else "Wi-Fi is on but not joined to a network."


@tool(DESTRUCTIVE)
def set_wifi(on: bool) -> str:
    """Turn Wi-Fi on or off. Turning it off cuts the internet, including Sommus's own connection.

    Args:
        on: True to turn Wi-Fi on, False to turn it off.
    """
    on_now, ssid = macos.set_wifi(on)
    return f"Wi-Fi is now {'on' if on_now else 'off'}{f', on {ssid}' if ssid else ''}."


@tool(REVERSIBLE)
def press_keys(combo: str) -> str:
    """Send a keyboard shortcut to whatever app is in front — for actions with no dedicated tool.

    Args:
        combo: Keys joined by "+", e.g. "cmd+s", "cmd+shift+t", "cmd+w", "escape", "left".
    """
    return f"Pressed {macos.press_keys(combo)}."


# ---------------------------------------------------------------- music, shortcuts, reminders


@tool(READ)
def get_now_playing() -> str:
    """Get the track and artist currently playing in Spotify or Music."""
    return apps.now_playing()


@tool(READ)
def list_shortcuts() -> str:
    """List the macOS Shortcuts available to run. Use this to find capabilities Sommus has no tool for."""
    names = apps.list_shortcuts()
    return f"{len(names)} shortcuts: {', '.join(names)}." if names else "No shortcuts found."


@tool(REVERSIBLE)
def run_shortcut(name: str) -> str:
    """Run one of the user's macOS Shortcuts. This covers things with no dedicated tool, such as Focus modes.

    Args:
        name: The shortcut's name — call list_shortcuts first if unsure.
    """
    return f"Ran the '{apps.run_shortcut(name)}' shortcut."


@tool(REVERSIBLE)
def create_reminder(title: str, due: str | None = None, list_name: str | None = None) -> str:
    """Add a reminder in the Reminders app. Unlike a notification, this persists and syncs to the iPhone.

    Args:
        title: What to be reminded about.
        due: Optional date and time as "2026-09-14 20:00". Omit for a reminder with no time.
        list_name: Optional Reminders list; omitted means the default list.
    """
    return f"Reminder added: {apps.create_reminder(title, due, list_name)}."


@tool(READ)
def list_reminders(limit: int = 10) -> str:
    """List the user's unfinished reminders.

    Args:
        limit: How many to return (default 10).
    """
    names = apps.list_reminders(limit)
    return "Reminders: " + "; ".join(names) if names else "Nothing on the reminders list."


# ---------------------------------------------------------------- clipboard, files


@tool(READ)
def get_clipboard() -> str:
    """Read what's currently on the clipboard."""
    text = files.clipboard_get()
    return f"Clipboard: {text}" if text.strip() else "The clipboard is empty."


@tool(REVERSIBLE)
def set_clipboard(text: str) -> str:
    """Put text on the clipboard, ready to paste.

    Args:
        text: The text to copy.
    """
    files.clipboard_set(text)
    return "Copied to the clipboard."


@tool(READ)
def find_files(query: str, limit: int = 10, folder: str | None = None) -> str:
    """Search the Mac for files and folders by name (Spotlight).

    Args:
        query: Part of the file name, e.g. "ECE 150", "resume", "lab 2".
        limit: How many results (default 10).
        folder: Optional folder to search inside, e.g. "~/Documents".
    """
    found = files.find_files(query, limit, folder)
    return "Found:\n" + "\n".join(str(p) for p in found) if found else f"Nothing named like '{query}'."


@tool(READ)
def read_file(path: str) -> str:
    """Read a text file, or list a folder's contents. Long files are truncated.

    Args:
        path: Full path, e.g. "~/Documents/notes.md".
    """
    return files.read_text_file(path)


@tool(REVERSIBLE)
def append_to_file(path: str, text: str) -> str:
    """Add a line to the end of a text file, creating the file if needed.

    Args:
        path: Full path to the file.
        text: The line to append.
    """
    return f"Added to {files.append_text_file(path, text)}."


@tool(REVERSIBLE)
def open_file(path: str) -> str:
    """Open a file or folder in its default app (Finder for folders, Preview for PDFs...).

    Args:
        path: Full path to the file or folder.
    """
    return f"Opened {files.open_path(path)}."


# ---------------------------------------------------------------- browser


@tool(READ)
def list_browser_tabs() -> str:
    """List every open Chrome tab with its window.tab number, title and URL."""
    tabs = browser.list_tabs()
    return f"{len(tabs)} tabs:\n" + "\n".join(str(t) for t in tabs) if tabs else "No tabs open."


@tool(READ)
def read_browser_tab(tab: str) -> str:
    """Read the visible text of a Chrome tab — the way to see what's on a page.

    Args:
        tab: Tab number ("3"), window.tab ("1.3"), or text from its title or URL ("MATH 115").
    """
    found, text = browser.read_tab(tab)
    return f"{found.title} ({found.url}):\n\n{text}"


@tool(READ)
def list_page_links(tab: str, contains: str | None = None) -> str:
    """List a page's links and buttons as "text -> url". Use this instead of guessing where to click.

    Args:
        tab: Tab number, window.tab, or text from its title or URL.
        contains: Optional filter, e.g. "L3" or "download".
    """
    found, links = browser.list_links(tab, contains)
    if not links:
        return f"No links{f' matching {contains!r}' if contains else ''} on '{found.title}'."
    return f"Links on {found.title}:\n" + "\n".join(links)


@tool(REVERSIBLE)
def click_page_link(tab: str, text: str) -> str:
    """Click a link or button on a page by its visible text — works inside web apps like LEARN and Gmail.

    Args:
        tab: Tab number, window.tab, or text from its title or URL.
        text: Visible text of the link or button, e.g. "MATH117-L3-F26-Fong".
    """
    found, result = browser.click_link(tab, text)
    return f"{result} (on {found.title})"


@tool(REVERSIBLE)
def compose_email(to: str, subject: str, body: str, send: bool = False) -> str:
    """Write an email in Gmail. There is no Mail app tool — Gmail in the browser is how email works here.

    Args:
        to: Recipient address.
        subject: Subject line.
        body: Message text.
        send: False opens a draft for review; True sends it immediately.
    """
    return browser.compose_gmail(to, subject, body, send)


@tool(REVERSIBLE)
def focus_browser_tab(tab: str) -> str:
    """Bring a Chrome tab to the front.

    Args:
        tab: Tab number, window.tab, or text from its title or URL.
    """
    return f"Switched to {browser.focus_tab(tab).title}."


# ---------------------------------------------------------------- typing, screen, downloads


@tool(REVERSIBLE)
def type_text(text: str, press_return: bool = False) -> str:
    """Type text into whatever app has focus — for writing into any app.

    Don't use this to run shell commands; use run_shell, which returns the actual output.

    Args:
        text: The literal text to type.
        press_return: True to press Return afterwards (submits the line).
    """
    typed = macos.type_text(text, press_return)
    return f"Typed {typed} characters{' and pressed Return' if press_return else ''}."


@tool(DESTRUCTIVE)
def run_shell(command: str, timeout: float = 60) -> str:
    """Run a shell command and read its output. The way to do anything with no dedicated tool.

    Commands run as Jashan from his home folder. sudo won't work — there's nobody to type a password.

    Args:
        command: The command line, e.g. "ls ~/Downloads", "git -C ~/x status", "brew list | head".
        timeout: Seconds to allow before giving up (default 60).
    """
    output, code = macos.run_shell(command, timeout)
    if code != 0:
        return f"Exit code {code}:\n{output or '(no output)'}"
    return output or "(done, no output)"


@tool(READ)
def screenshot() -> list:
    """Take a screenshot and look at it. Use this to see an app's state, then click what you see.

    Click coordinates refer to this image.
    """
    path, width, height = macos.screenshot()
    return [Image(path=path), f"Screenshot is {width}x{height}; click coordinates refer to this image."]


@tool(REVERSIBLE)
def click(x: int, y: int, double: bool = False) -> str:
    """Click somewhere on screen, using coordinates from the most recent screenshot.

    Args:
        x: Horizontal position in the screenshot.
        y: Vertical position in the screenshot.
        double: True for a double-click.
    """
    at = macos.click(x, y, double)
    return f"{'Double-clicked' if double else 'Clicked'} at {at[0]}, {at[1]}."


@tool(READ)
def wait(seconds: float) -> str:
    """Pause before the next step — for pages, apps or downloads that need a moment.

    Args:
        seconds: How long to wait, up to 10.
    """
    import time

    if not 0 < seconds <= 10:
        raise ToolError("Wait between 0 and 10 seconds.")
    time.sleep(seconds)
    return f"Waited {seconds:g}s."


@tool(REVERSIBLE)
def download_url(url: str, folder: str = "~/Downloads", filename: str | None = None) -> str:
    """Download a file straight to disk. Works for public URLs; pages behind a login don't.

    Args:
        url: Direct http(s) link to the file.
        folder: Where to save it (default ~/Downloads).
        filename: Optional name to save it as.
    """
    return f"Saved to {files.download_url(url, folder, filename)}."


# ---------------------------------------------------------------- messaging


@tool(DESTRUCTIVE)
def send_message(to: str, text: str) -> str:
    """Send an iMessage. This reaches another person and can't be unsent.

    Args:
        to: Phone number or Apple ID of the recipient.
        text: The message body.
    """
    return f"Sent to {apps.send_message(to, text)}."


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
