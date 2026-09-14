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
from mcp.types import ToolAnnotations

from sommus.nodes.laptop import macos

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
    """Get the current output volume (0-100) and whether sound is muted."""
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
    """Show a macOS notification banner.

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


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
