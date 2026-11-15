"""One list of plugs across every configured brand (SwitchBot, Tuya/Merkury), so "the lamp" finds its plug."""

from __future__ import annotations

from typing import Any

from sommus.nodes.home import switchbot, tuya
from sommus.nodes.home.switchbot import HomeError


def providers() -> list:
    """Configured brands. SwitchBot is the default when nothing is configured, so its setup message shows."""
    found = [tuya] if tuya.configured() else []
    if switchbot.configured() or not found:
        found.append(switchbot)
    return found


def all_plugs() -> list[dict[str, Any]]:
    return [{**d, "provider": mod} for mod in providers() for d in mod.plugs()]


def _pick(name: str) -> dict[str, Any]:
    found = all_plugs()
    if not found and tuya.configured() and not switchbot.configured():
        raise HomeError("No plug is on the linked Tuya account yet. Add it in the Tuya Smart / Smart Life app first.")
    return switchbot.pick(name, found)


def names() -> list[str]:
    return [d["deviceName"] for d in all_plugs()]


def turn(name: str, on: bool) -> str:
    plug = _pick(name)
    return plug["provider"].turn_plug(plug, on)


def status(name: str) -> str:
    plug = _pick(name)
    return plug["provider"].status_plug(plug)
