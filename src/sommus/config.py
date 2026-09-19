"""Loads config.toml. Secrets never live here — they go in .env."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sommus.brain.permissions import Tier

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class NodeConfig:
    name: str
    module: str  # launched as `python -m <module>` over stdio
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    name: str
    user: str
    model: str
    effort: str
    max_steps: int
    max_steps_hard: int
    web_search: bool
    nodes: tuple[NodeConfig, ...]
    overrides: dict[str, Tier]
    data_dir: Path
    ask_before_destructive: bool = False
    core_tools: tuple[str, ...] = ()
    history_turns: int = 12
    fast_path: bool = True
    pin_tools: tuple[str, ...] = ()  # personal tools that wait for the PIN
    unlock_minutes: float = 10
    memory_path: Path | None = None  # long-term facts Sommus writes itself (brain/memory.py)


def section(name: str, path: Path | None = None) -> dict:
    """A raw config table, for interfaces with their own settings (e.g. [voice])."""
    path = path or Path(os.environ.get("SOMMUS_CONFIG", ROOT / "config.toml"))
    return tomllib.loads(path.read_text()).get(name, {})


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("SOMMUS_CONFIG", ROOT / "config.toml"))
    raw = tomllib.loads(path.read_text())
    assistant = raw["assistant"]
    return Config(
        name=assistant["name"],
        user=assistant["user"],
        model=assistant["model"],
        effort=assistant.get("effort", "medium"),
        max_steps=assistant.get("max_steps", 20),
        max_steps_hard=assistant.get("max_steps_hard", 60),
        web_search=assistant.get("web_search", True),
        core_tools=tuple(assistant.get("core_tools", [])),
        history_turns=assistant.get("history_turns", 12),
        fast_path=assistant.get("fast_path", True),
        nodes=tuple(
            NodeConfig(n["name"], n["module"], {k: os.path.expandvars(v) for k, v in n.get("env", {}).items()})
            for n in raw.get("nodes", [])
        ),
        overrides={tool: Tier(tier) for tool, tier in raw.get("permissions", {}).items()},
        data_dir=ROOT / "data",
        ask_before_destructive=raw.get("safety", {}).get("ask_before_destructive", False),
        pin_tools=tuple(raw.get("security", {}).get("pin_tools", [])),
        unlock_minutes=float(raw.get("security", {}).get("unlock_minutes", 10)),
        memory_path=Path(raw["memory"]["path"]).expanduser() if raw.get("memory", {}).get("path") else None,
    )
