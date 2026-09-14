"""SQLite audit log: every turn, every tool call, and what it cost."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# $ per million tokens (input, output). Cache writes bill at 1.25× input, reads at 0.1×.
PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, usage: Any) -> None:
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def cost_usd(self, model: str) -> float:
        in_price, out_price = PRICES.get(model, PRICES["claude-opus-5"])
        billed_input = self.input_tokens + 1.25 * self.cache_write_tokens + 0.1 * self.cache_read_tokens
        return (billed_input * in_price + self.output_tokens * out_price) / 1_000_000


SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    user_text TEXT NOT NULL,
    reply TEXT,
    status TEXT,
    model TEXT,
    input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    cost_usd REAL
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY,
    turn_id INTEGER NOT NULL REFERENCES turns(id),
    at TEXT NOT NULL,
    tool TEXT NOT NULL,
    input TEXT NOT NULL,
    tier TEXT,
    decision TEXT NOT NULL,
    is_error INTEGER NOT NULL,
    output TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)

    def start_turn(self, user_text: str) -> int:
        cur = self._db.execute("INSERT INTO turns (started_at, user_text) VALUES (?, ?)", (_now(), user_text))
        self._db.commit()
        return cur.lastrowid

    def log_tool(
        self, turn_id: int, tool: str, input: dict, tier: str | None, decision: str, is_error: bool, output: str
    ) -> None:
        self._db.execute(
            "INSERT INTO tool_calls (turn_id, at, tool, input, tier, decision, is_error, output)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (turn_id, _now(), tool, json.dumps(input), tier, decision, int(is_error), output),
        )
        self._db.commit()

    def finish_turn(self, turn_id: int, reply: str, status: str, model: str, usage: Usage) -> None:
        self._db.execute(
            "UPDATE turns SET reply=?, status=?, model=?, input_tokens=?, output_tokens=?,"
            " cache_read_tokens=?, cache_write_tokens=?, cost_usd=? WHERE id=?",
            (
                reply,
                status,
                model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                usage.cost_usd(model),
                turn_id,
            ),
        )
        self._db.commit()

    def cost_today(self) -> tuple[int, float]:
        row = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM turns WHERE started_at >= ?",
            (datetime.now().strftime("%Y-%m-%d"),),
        ).fetchone()
        return row[0], row[1]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
