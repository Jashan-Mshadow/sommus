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
from sommus.brain.nodes import NodeHub, ToolOutput
from sommus.brain.permissions import Tier
from sommus.brain.store import Store
from sommus.config import ROOT, Config

# Server-side helpers the search tool drives itself — not Sommus's choices.
IGNORED_TOOLS = {"code_execution", "tool_search_tool_bm25"}


@dataclass(frozen=True)
class Case:
    text: str
    expect: tuple[str, ...] | None  # None = no expectation: any sensible answer passes


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
        return [tool for tool in (self.case.expect or ()) if tool not in self.called]

    @property
    def extra(self) -> list[str]:
        expected = self.case.expect or ()
        return [t for t in self.called if t not in expected and t not in IGNORED_TOOLS]

    @property
    def passed(self) -> bool:
        if self.case.expect is None:
            return not self.notices  # open-ended: anything that didn't error
        if not self.case.expect:
            return not [t for t in self.called if t not in IGNORED_TOOLS]  # must answer, not act
        return not self.missing


def load_cases(path: Path | None = None) -> list[Case]:
    path = path or ROOT / "evals" / "commands.toml"
    raw = tomllib.loads(path.read_text())
    return [Case(text=c["text"], expect=tuple(c["expect"]) if "expect" in c else None) for c in raw["command"]]


class SimulatingHub:
    """Wraps the real hub: read-only tools run, the rest report a fake success."""

    def __init__(self, hub: NodeHub):
        self._hub = hub
        self.executed: list[str] = []

    def api_tools(self, core: tuple[str, ...] | set[str] = ()) -> list[dict[str, Any]]:
        return self._hub.api_tools(core)

    def tier(self, tool_name: str) -> Tier | None:
        return self._hub.tier(tool_name)

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> ToolOutput:
        if self._hub.tier(tool_name) is Tier.READ:
            self.executed.append(tool_name)
            return await self._hub.call(tool_name, arguments)
        message = f"Done. ({tool_name} was simulated for the eval, not actually run.)"
        return ToolOutput([{"type": "text", "text": message}], message, False)


async def always_allow(name: str, args: dict[str, Any]) -> bool:
    return True


async def run_case(cfg: Config, hub: NodeHub | SimulatingHub, store: Store, case: Case, client: Any = None) -> Result:
    brain = Brain(cfg, hub, store, client=client)  # fresh conversation per command
    result = Result(case=case)
    started = time.monotonic()
    after_tool = False
    async for event in brain.handle(case.text, always_allow):
        match event:
            case ToolStarted(name=name):
                result.called.append(name)
                after_tool = True
            case TextDelta(text=text):
                if after_tool and result.reply:
                    result.reply += "\n"
                    after_tool = False
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
                        "expect": list(r.case.expect) if r.case.expect is not None else None,
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
