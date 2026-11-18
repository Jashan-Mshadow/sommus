"""The smart plugs, through SwitchBot (the only brand Sommus uses since 2026-11-17).

Kept as its own small layer so a second brand could be added later without touching the tools.
"""

from __future__ import annotations

from typing import Any

from sommus.nodes.home import switchbot
from sommus.nodes.home.switchbot import HomeError  # noqa: F401  (re-exported for callers)


def all_plugs() -> list[dict[str, Any]]:
    return [{**d, "provider": switchbot} for d in switchbot.plugs()]


def names() -> list[str]:
    return [d["deviceName"] for d in all_plugs()]


def turn(name: str, on: bool) -> str:
    plug = switchbot.pick(name, all_plugs())
    return plug["provider"].turn_plug(plug, on)


def status(name: str) -> str:
    plug = switchbot.pick(name, all_plugs())
    return plug["provider"].status_plug(plug)
