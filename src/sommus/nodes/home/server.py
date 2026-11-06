"""Home node: smart plugs through the SwitchBot cloud, so a fan or a lamp can be switched from anywhere.

Runs wherever the brain runs (the always-on server), not on the Mac. On/off is reversible, so it runs without
asking; the same rules as every other tool still apply: the PIN gate, and the outside-content rule, which
keeps something read in an email or on a page from switching things on its own.
"""

from __future__ import annotations

import functools
from collections.abc import Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from sommus.nodes.home import switchbot

READ = ToolAnnotations(read_only_hint=True)
REVERSIBLE = ToolAnnotations(read_only_hint=False, destructive_hint=False)

server = MCPServer(
    "sommus-home", instructions="Switches Jashan's smart plugs (his fan) on and off.", log_level="WARNING"
)


def tool(annotations: ToolAnnotations) -> Callable:
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except switchbot.HomeError as e:
                raise ToolError(str(e)) from e
            except Exception as e:  # never let a tool fail with no explanation
                raise ToolError(f"{type(e).__name__}: {e}") from e

        server.tool(annotations=annotations, structured_output=False)(wrapper)
        return fn

    return decorator


@tool(READ)
def list_plugs() -> str:
    """List the smart plugs on Jashan's SwitchBot account, with whether each is on."""
    found = switchbot.plugs()
    if not found:
        return "No SwitchBot plugs on the account yet."
    return "\n".join(switchbot.status(d["deviceName"]) for d in found)


@tool(READ)
def plug_status(plug: str = "") -> str:
    """Is a smart plug on, and how much power is it drawing? Use this to check that a switch really happened.

    Args:
        plug: The plug's name in the SwitchBot app, e.g. "fan". Leave empty if there is only one.
    """
    return switchbot.status(plug)


@tool(REVERSIBLE)
def plug_on(plug: str = "") -> str:
    """Turn a smart plug on, e.g. "turn on the fan". Works from anywhere, through the internet.

    Args:
        plug: The plug's name in the SwitchBot app, e.g. "fan". Leave empty if there is only one.
    """
    return switchbot.turn(plug, True)


@tool(REVERSIBLE)
def plug_off(plug: str = "") -> str:
    """Turn a smart plug off, e.g. "turn off the fan". Works from anywhere, through the internet.

    Args:
        plug: The plug's name in the SwitchBot app, e.g. "fan". Leave empty if there is only one.
    """
    return switchbot.turn(plug, False)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
