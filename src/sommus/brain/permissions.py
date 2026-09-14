"""Permission tiers — enforced in code, never left to the prompt.

A node declares each tool's tier through MCP annotations. `config.toml` can
override any tool. A tool with no annotations is treated as destructive, so a
forgotten annotation fails safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from mcp.types import ToolAnnotations


class Tier(StrEnum):
    READ = "read"  # run immediately
    REVERSIBLE = "reversible"  # run, report back
    DESTRUCTIVE = "destructive"  # ask the user first, unless full permission is on
    ALWAYS_ASK = "always_ask"  # ask every time, even with full permission — outward-facing actions
    BLOCKED = "blocked"  # never run; hidden from the model


def tier_from_annotations(annotations: ToolAnnotations | None) -> Tier:
    if annotations is None:
        return Tier.DESTRUCTIVE
    if annotations.read_only_hint:
        return Tier.READ
    if annotations.destructive_hint is False:
        return Tier.REVERSIBLE
    return Tier.DESTRUCTIVE


@dataclass(frozen=True)
class Policy:
    overrides: dict[str, Tier] = field(default_factory=dict)

    def tier_for(self, tool_name: str, annotations: ToolAnnotations | None) -> Tier:
        return self.overrides.get(tool_name) or tier_from_annotations(annotations)
