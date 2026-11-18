"""Shared pieces: the example record, JSONL helpers, the target a model should hit, and the split."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

# A request is "easy" when Claude answered it with one or two tool calls that worked. Everything else (no tool,
# a long chain, an error, or a call that is itself a hand-off to another model) is what the small model should
# hand back to Claude: its target is "escalate".
MAX_EASY_CALLS = 2
HAND_OFF_TOOLS = frozenset({"ask_claude", "ask_gemini", "ask_gpt", "web_search", "run_shell"})
TEST_PERCENT = 20


def key(request: str) -> str:
    """The identity of a request: case, spacing and end punctuation don't make it a different request."""
    return re.sub(r"\s+", " ", request.strip().casefold()).strip(" .!?,;:")


def is_test(request: str, percent: int = TEST_PERCENT) -> bool:
    """Deterministic split by hash of the request text: the same request always lands in the same set."""
    digest = hashlib.sha256(key(request).encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100 < percent


def target(example: dict) -> dict:
    """What the small model should answer for this example: {"escalate": True} or {"calls": [...]}."""
    calls = example.get("calls") or []
    if example.get("source") == "synthetic":
        return {"calls": calls} if calls else {"escalate": True}
    easy = (
        1 <= len(calls) <= MAX_EASY_CALLS
        and not example.get("error")
        and not any(c["tool"] in HAND_OFF_TOOLS for c in calls)
    )
    return {"calls": calls} if easy else {"escalate": True}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a half-written line from a killed run is not worth stopping for
    return out


def append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def write_json(path: Path, data: object) -> None:
    """Atomic: a killed run never leaves a half-written manifest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)
