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
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0  # the part of cache_write_tokens written with a 1-hour TTL
    web_searches: int = 0
    extra_usd: float = 0.0  # spend outside the main model call, e.g. the search worker

    def add(self, usage: Any) -> None:
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        breakdown = getattr(usage, "cache_creation", None)
        self.cache_write_1h_tokens += getattr(breakdown, "ephemeral_1h_input_tokens", 0) or 0
        server = getattr(usage, "server_tool_use", None)
        self.web_searches += getattr(server, "web_search_requests", 0) or 0

    def cost_usd(self, model: str) -> float:
        in_price, out_price = PRICES.get(model, PRICES["claude-opus-5"])
        # Writes cost 1.25x input with the 5-minute TTL and 2x with the 1-hour one; reads 0.1x.
        writes_5m = self.cache_write_tokens - self.cache_write_1h_tokens
        billed_input = (
            self.input_tokens + 1.25 * writes_5m + 2.0 * self.cache_write_1h_tokens + 0.1 * self.cache_read_tokens
        )
        tokens = (billed_input * in_price + self.output_tokens * out_price) / 1_000_000
        return tokens + WEB_SEARCH_USD * self.web_searches + self.extra_usd


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

    def fast_path_candidates(self, limit: int = 30) -> list[tuple[str, str, int, float]]:
        """Requests the model answered with exactly one tool call — the next fast-path intents.

        Returns (tool, example request, times asked, total spent), most-asked first. Anything the
        model handled with a single tool is, by definition, a basic command worth moving to $0.
        """
        rows = self._db.execute(
            """
            SELECT c.tool, lower(t.user_text), COUNT(*) AS n, SUM(t.cost_usd)
            FROM turns t JOIN tool_calls c ON c.turn_id = t.id
            WHERE t.model != 'fastpath' AND t.status = 'ok'
              AND (SELECT COUNT(*) FROM tool_calls x WHERE x.turn_id = t.id) = 1
            GROUP BY c.tool, lower(t.user_text)
            ORDER BY n DESC, SUM(t.cost_usd) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [(tool, text, count, spent or 0.0) for tool, text, count, spent in rows]

    def cost_month(self) -> float:
        """Spend since the 1st, the window Anthropic's monthly limit counts."""
        row = self._db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM turns WHERE started_at >= ?",
            (datetime.now().strftime("%Y-%m-01"),),
        ).fetchone()
        return row[0]

    def cost_today(self) -> tuple[int, float]:
        row = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM turns WHERE started_at >= ?",
            (datetime.now().strftime("%Y-%m-%d"),),
        ).fetchone()
        return row[0], row[1]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
