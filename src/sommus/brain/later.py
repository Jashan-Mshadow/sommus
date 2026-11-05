"""Mac actions that wait for the Mac: asked while it sleeps, run when it wakes.

On the server, a Mac tool used to answer "the MacBook isn't reachable" and that was the end of it. For a few
small things that can just as well happen later (a reminder in Apple's Reminders, a page to open, a
notification) that's a wasted request: they wait here (data/mac_queue.json, so a server restart keeps them)
and the server's scheduler runs them once the Mac's node is back, then tells Jashan on Telegram.

Only tools listed in [mac_queue] wait, and never a personal (PIN-gated) one, whatever the list says; the hub
also refuses anything not in the reversible tier. Anything that waited longer than `hours` is dropped, not run:
opening a page two days late is worse than not opening it.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass
class Waiting:
    node: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    asked: str = ""  # "2026-10-08 13:05", local time

    def describe(self) -> str:
        shown = ", ".join(f"{v}" for v in self.args.values() if v not in (None, ""))
        shown = shown if len(shown) <= 80 else shown[:77] + "..."
        return f"{self.tool} ({shown})" if shown else self.tool


class Later:
    def __init__(
        self,
        path: Path,
        tools: Iterable[str],
        personal: Iterable[str] = (),
        hours: float = 24,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.path, self.clock = path, clock
        self.tools = frozenset(tools) - frozenset(personal)
        self.max_age = timedelta(hours=hours)
        self.items: list[Waiting] = []
        with contextlib.suppress(OSError, ValueError, TypeError):
            self.items = [Waiting(**w) for w in json.loads(path.read_text())]

    def can_wait(self, tool: str) -> bool:
        return tool in self.tools

    def save(self) -> None:
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps([asdict(w) for w in self.items], indent=1))

    def add(self, node: str, tool: str, args: dict[str, Any]) -> Waiting:
        """Queue a call; asking for the same thing twice queues it once."""
        for waiting in self.items:
            if (waiting.node, waiting.tool, waiting.args) == (node, tool, args):
                return waiting
        waiting = Waiting(node, tool, dict(args), f"{self.clock():%Y-%m-%d %H:%M}")
        self.items.append(waiting)
        self.save()
        return waiting

    def put_back(self, items: list[Waiting]) -> None:
        """Calls taken to run that couldn't (the Mac fell asleep again): first in line next time."""
        self.items[:0] = items
        self.save()

    def nodes(self) -> set[str]:
        return {w.node for w in self.items}

    def take(self, node: str) -> tuple[list[Waiting], list[Waiting]]:
        """Everything waiting for `node`, removed from the queue: (still worth running, too old to run)."""
        now = self.clock()
        mine = [w for w in self.items if w.node == node]
        if not mine:
            return [], []
        self.items = [w for w in self.items if w.node != node]
        self.save()
        fresh, stale = [], []
        for waiting in mine:
            with contextlib.suppress(ValueError):
                if now - datetime.strptime(waiting.asked, "%Y-%m-%d %H:%M") <= self.max_age:
                    fresh.append(waiting)
                    continue
            stale.append(waiting)
        return fresh, stale
