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
    nodes: tuple[NodeConfig, ...]
    overrides: dict[str, Tier]
    data_dir: Path
    ask_before_destructive: bool = False


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("SOMMUS_CONFIG", ROOT / "config.toml"))
    raw = tomllib.loads(path.read_text())
    assistant = raw["assistant"]
    return Config(
        name=assistant["name"],
        user=assistant["user"],
        model=assistant["model"],
        effort=assistant.get("effort", "medium"),
        max_steps=assistant.get("max_steps", 12),
        nodes=tuple(NodeConfig(n["name"], n["module"], n.get("env", {})) for n in raw.get("nodes", [])),
        overrides={tool: Tier(tier) for tool, tier in raw.get("permissions", {}).items()},
        data_dir=ROOT / "data",
        ask_before_destructive=raw.get("safety", {}).get("ask_before_destructive", False),
    )
