"""`sommus export-training`: every request, the tools it used and the reply, as JSONL, for training a small model.

The plan (vault: "Sommus — Future Plans"): a small local model handles the easy requests and hands the rest
to Claude; every request Claude handles becomes a training example for it. This is the data side: one clean
record per turn, from the same SQLite log the rest of Sommus already writes.

It stays on the machine (`data/` is gitignored): requests and tool outputs hold personal text. PIN turns are
left out, and a tool output is cut short, since a long email or note adds nothing to learning which tool to call.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

OUTPUT_CLIP = 600


def records(db: sqlite3.Connection, after_id: int = 0, before_id: int | None = None) -> list[dict]:
    """Every turn with id > after_id (and < before_id when given): the nightly model run reads only what is new."""
    db.row_factory = sqlite3.Row
    turns = db.execute(
        "SELECT id, started_at, user_text, reply, status, model, cost_usd FROM turns "
        "WHERE model != 'pin' AND user_text NOT LIKE '[PIN entered]%' AND id > ? AND id < ? ORDER BY id",
        (after_id, before_id if before_id is not None else 2**62),
    ).fetchall()
    out = []
    for turn in turns:
        calls = db.execute(
            "SELECT tool, input, decision, is_error, output FROM tool_calls WHERE turn_id = ? ORDER BY id",
            (turn["id"],),
        ).fetchall()
        out.append(
            {
                "id": turn["id"],
                "at": turn["started_at"],
                "request": turn["user_text"],
                "route": "fastpath" if turn["model"] == "fastpath" else "model",
                "model": turn["model"],
                "status": turn["status"],
                "cost_usd": turn["cost_usd"] or 0.0,
                "tools": [
                    {
                        "tool": c["tool"],
                        "args": _args(c["input"]),
                        "decision": c["decision"],
                        "error": bool(c["is_error"]),
                        "output": (c["output"] or "")[:OUTPUT_CLIP],
                    }
                    for c in calls
                ],
                "reply": turn["reply"] or "",
            }
        )
    return out


def _args(raw: str) -> object:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def write(db_path: Path, out_path: Path) -> int:
    """Write the JSONL file; returns how many records."""
    with sqlite3.connect(db_path) as db:
        found = records(db)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in found))
    return len(found)
