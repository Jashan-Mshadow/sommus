"""Fixed commands that never reach the model: $0 and instant.

A phone command like "brightness 20" was costing ~5¢, nearly all of it re-sending the
prompt. These patterns are anchored and strict on purpose — anything with a relative
amount ("a bit brighter"), extra words, or ambiguity falls through to the model, so a
false match is far less likely than a missed one, and a miss only costs what it did before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

POLITE = r"(?:please\s+|pls\s+|hey\s+sommus,?\s+|sommus,?\s+)?"
END = r"\s*(?:please|pls)?\s*[.!?]*\s*$"
DEVICE = r"(?:(?:my|the)\s+)?(?:laptop\s+|mac\s+|macbook\s+|screen\s+|display\s+|computer\s+)?"


@dataclass(frozen=True)
class Match:
    tool: str
    args: dict[str, Any]


def _level(text: str) -> int | None:
    value = int(text)
    return value if 0 <= value <= 100 else None


RULES: list[tuple[re.Pattern[str], Any]] = [
    (
        re.compile(
            rf"^{POLITE}(?:set|turn|make|change|put)?\s*{DEVICE}brightness\s*(?:to|at|=)?\s*(\d{{1,3}})\s*%?{END}"
        ),
        lambda m: (lvl := _level(m.group(1))) is not None and Match("set_brightness", {"level": lvl}),
    ),
    (
        re.compile(rf"^{POLITE}(?:set|turn|make|change|put)?\s*{DEVICE}volume\s*(?:to|at|=)?\s*(\d{{1,3}})\s*%?{END}"),
        lambda m: (lvl := _level(m.group(1))) is not None and Match("set_volume", {"level": lvl}),
    ),
    (
        re.compile(rf"^{POLITE}mute(?:\s+{DEVICE}(?:volume|sound|audio|it))?{END}"),
        lambda m: Match("set_mute", {"muted": True}),
    ),
    (
        re.compile(rf"^{POLITE}unmute(?:\s+{DEVICE}(?:volume|sound|audio|it))?{END}"),
        lambda m: Match("set_mute", {"muted": False}),
    ),
    (
        re.compile(rf"^{POLITE}(?:pause|play|resume)(?:\s+(?:the\s+)?(?:music|song|spotify))?{END}"),
        lambda m: Match("media_control", {"action": "play_pause"}),
    ),
    (
        re.compile(rf"^{POLITE}(?:skip|next)(?:\s+(?:this\s+|the\s+)?(?:song|track))?{END}"),
        lambda m: Match("media_control", {"action": "next"}),
    ),
    (
        re.compile(rf"^{POLITE}(?:previous|last|go\s+back)(?:\s+(?:song|track))?{END}"),
        lambda m: Match("media_control", {"action": "previous"}),
    ),
    (
        re.compile(rf"^{POLITE}lock(?:\s+(?:my\s+|the\s+)?(?:laptop|mac|macbook|screen|computer))?{END}"),
        lambda m: Match("lock_screen", {}),
    ),
    (
        re.compile(rf"^{POLITE}(?:turn\s+off|sleep)\s+(?:the\s+|my\s+)?(?:screen|display|monitor){END}"),
        lambda m: Match("sleep_display", {}),
    ),
    (
        re.compile(
            rf"^{POLITE}(?:what'?s\s+|what\s+is\s+|how'?s\s+|how\s+is\s+|check\s+)?(?:my\s+|the\s+)?battery(?:\s+(?:level|at|percentage|life))?{END}"
        ),
        lambda m: Match("get_battery", {}),
    ),
]


def match(text: str) -> Match | None:
    normalized = " ".join(text.strip().lower().split())
    for pattern, build in RULES:
        found = pattern.match(normalized)
        if found:
            result = build(found)
            return result or None
    return None
