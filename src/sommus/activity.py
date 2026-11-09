"""The daily activity log: what Sommus did and said each day, as a vault note people (and Claude) can review.

Two sources: the turn log in SQLite (every request, its tool calls, the reply, cost) and `data/said.jsonl`, the
things Sommus said first (brief, nudges, reminders) and where each went. The server rewrites today's note in the
vault every few minutes; the vault sync pushes it. Plain code, no model, $0. The vault repo is private; this
never goes to the public stats page.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from pathlib import Path

FOLDER = "Sommus Log"
CLIP = 300
RAN = ("ran",)  # brain/loop.py: anything else is needs_pin, blocked, declined or unknown


def record_said(path: Path, kind: str, text: str, route: str, now: datetime | None = None) -> None:
    """One line per thing Sommus said without being asked: kind (brief, nudge, reminder...), route (spoken,
    telegram, both)."""
    now = now or datetime.now()
    with contextlib.suppress(OSError), path.open("a") as f:
        f.write(json.dumps({"at": now.isoformat(timespec="seconds"), "kind": kind, "route": route, "text": text}))
        f.write("\n")


def said_on(path: Path, day: str) -> list[dict]:
    out = []
    with contextlib.suppress(OSError):
        for line in path.read_text().splitlines():
            with contextlib.suppress(ValueError):
                item = json.loads(line)
                if str(item.get("at", "")).startswith(day):
                    out.append(item)
    return out


def _clip(text: str, limit: int = CLIP) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _clock(stamp: str) -> str:
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(stamp).strftime("%H:%M")
    return stamp[11:16]


def day_note(day: str, turns: list[dict], said: list[dict]) -> str:
    """The Markdown note for one day: a summary, then everything in time order."""
    cost = sum(t["cost"] for t in turns)
    failed = [t for t in turns if t["status"] not in ("ok", "")]
    tool_errors = sum(1 for t in turns for c in t["tools"] if c["error"])
    denied = sum(1 for t in turns for c in t["tools"] if c["decision"] not in RAN)
    fast = sum(1 for t in turns if t["model"] == "fastpath")
    lines = [
        "---",
        f"date: {day}",
        "type: sommus-log",
        f"turns: {len(turns)}",
        f"cost_usd: {cost:.4f}",
        "tags: [sommus, log]",
        "---",
        f"# Sommus log {day}",
        "",
        "Written by the server (activity.py) from its turn log; rewritten through the day. Review notes go in "
        "[[Sommus Review]].",
        "",
        f"**Summary:** {len(turns)} requests ({fast} on the free fast path), ${cost:.4f}, {len(said)} said first, "
        f"{tool_errors} tool errors, {denied} tool calls not allowed or held, {len(failed)} turns not ok.",
        "",
    ]
    events = [(t["at"], "turn", t) for t in turns] + [(s["at"], "said", s) for s in said]
    for at, kind, item in sorted(events, key=lambda e: e[0]):
        if kind == "said":
            lines.append(f"- **{_clock(at)} said first** ({item.get('kind', '?')}, {item.get('route', '?')}): "
                         f"{_clip(item.get('text', ''))}")  # fmt: skip
            continue
        flag = "" if item["status"] in ("ok", "") else f" ⚠️ {item['status']}"
        lines.append(f"- **{_clock(at)} asked:** {_clip(item['text'], 200)}{flag}")
        for call in item["tools"]:
            mark = "✗" if call["error"] else "✓"
            held = "" if call["decision"] in RAN else f" [{call['decision']}]"
            detail = f": {_clip(call['output'], 120)}" if call["error"] else ""
            lines.append(f"  - {mark} `{call['tool']}`{held}{detail}")
        model = f" ({item['model']}, ${item['cost']:.4f})" if item["model"] else ""
        lines.append(f"  - replied{model}: {_clip(item['reply'])}")
    return "\n".join(lines) + "\n"


def write_day(vault: Path, day: str, turns: list[dict], said: list[dict]) -> Path:
    path = vault / FOLDER / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = day_note(day, turns, said)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
    return path
