"""Scores Sommus against evals/commands.toml.

Each command runs in a fresh conversation. A command passes when every expected
tool was called; a command with no expected tools passes only if Sommus called
nothing and said so instead.

Read-only tools run for real. Everything else is simulated by default, so
scoring 20 commands doesn't put the laptop to sleep halfway through. `--live`
runs them for real.
"""

from __future__ import annotations

import json
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sommus.brain.loop import Brain, Notice, TextDelta, ToolFinished, ToolStarted, TurnDone
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Tier
from sommus.brain.store import Store
from sommus.config import ROOT, Config


@dataclass(frozen=True)
class Case:
    text: str
    expect: tuple[str, ...]


@dataclass
class Result:
    case: Case
    called: list[str] = field(default_factory=list)
    reply: str = ""
    notices: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    seconds: float = 0.0

    @property
    def missing(self) -> list[str]:
        return [tool for tool in self.case.expect if tool not in self.called]

    @property
    def extra(self) -> list[str]:
        return [tool for tool in self.called if tool not in self.case.expect]

    @property
    def passed(self) -> bool:
        if not self.case.expect:
            return not self.called  # should have said "I can't do that"
        return not self.missing


def load_cases(path: Path | None = None) -> list[Case]:
    path = path or ROOT / "evals" / "commands.toml"
    raw = tomllib.loads(path.read_text())
    return [Case(text=c["text"], expect=tuple(c.get("expect", []))) for c in raw["command"]]


class SimulatingHub:
    """Wraps the real hub: read-only tools run, the rest report a fake success."""

    def __init__(self, hub: NodeHub):
        self._hub = hub
        self.executed: list[str] = []

    def api_tools(self) -> list[dict[str, Any]]:
        return self._hub.api_tools()

    def tier(self, tool_name: str) -> Tier | None:
        return self._hub.tier(tool_name)

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if self._hub.tier(tool_name) is Tier.READ:
            self.executed.append(tool_name)
            return await self._hub.call(tool_name, arguments)
        return f"Done. ({tool_name} was simulated for the eval, not actually run.)", False


async def always_allow(name: str, args: dict[str, Any]) -> bool:
    return True


async def run_case(cfg: Config, hub: NodeHub | SimulatingHub, store: Store, case: Case, client: Any = None) -> Result:
    brain = Brain(cfg, hub, store, client=client)  # fresh conversation per command
    result = Result(case=case)
    started = time.monotonic()
    async for event in brain.handle(case.text, always_allow):
        match event:
            case ToolStarted(name=name):
                result.called.append(name)
            case TextDelta(text=text):
                result.reply += text
            case Notice(text=text):
                result.notices.append(text)
            case TurnDone(cost_usd=cost):
                result.cost_usd = cost
            case ToolFinished():
                pass
    result.seconds = time.monotonic() - started
    return result


def save(results: list[Result], live: bool, cfg: Config) -> Path:
    path = cfg.data_dir / "evals" / f"{datetime.now():%Y-%m-%d_%H-%M-%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ran_at": datetime.now().isoformat(timespec="seconds"),
                "model": cfg.model,
                "effort": cfg.effort,
                "live": live,
                "passed": sum(r.passed for r in results),
                "total": len(results),
                "cost_usd": sum(r.cost_usd for r in results),
                "cases": [
                    {
                        "text": r.case.text,
                        "expect": list(r.case.expect),
                        "called": r.called,
                        "missing": r.missing,
                        "extra": r.extra,
                        "passed": r.passed,
                        "reply": r.reply,
                        "notices": r.notices,
                        "cost_usd": round(r.cost_usd, 5),
                        "seconds": round(r.seconds, 2),
                    }
                    for r in results
                ],
            },
            indent=2,
        )
    )
    return path
