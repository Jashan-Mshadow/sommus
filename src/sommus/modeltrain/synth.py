"""Stage 3: new realistic requests from Claude Haiku, aimed at the tools the data covers least.

The prompt holds only tool schemas and generic instructions, never a real request. Every generated example is
checked against the real schemas (tool names, argument names and types) and dropped if it does not fit. A hard
dollar cap per night is enforced before every call from a worst-case estimate, and again from real usage after.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sommus.brain.store import PRICES
from sommus.modeltrain import common, schemas
from sommus.modeltrain.secrets import looks_secret

CHARS_PER_TOKEN = 3.0  # pessimistic on purpose: the estimate should be above the bill, not below
BATCH = 20
MAX_TOKENS = 2000
SYSTEM = (
    "You write training data for a small assistant that turns spoken or typed requests into tool calls. "
    "You get tool schemas. For each tool, invent varied, natural requests a person would say to a personal "
    "assistant (short, casual, sometimes with typos or filler words), and the exact tool call(s) that fulfil them. "
    "Use only the tools and argument names in the schemas, with the right types. Never invent real people, "
    "phone numbers, passwords or addresses. Vary wording, length and which arguments are used.\n"
    'Answer with JSON only: a list of {"request": str, "calls": [{"tool": str, "args": {...}}]}.'
)


@dataclass
class SynthResult:
    status: str = "ok"  # ok | skipped
    reason: str = ""
    examples: list[dict] = field(default_factory=list)
    generated: int = 0
    invalid: int = 0
    duplicate: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    targets: list[str] = field(default_factory=list)


def skipped(reason: str) -> SynthResult:
    return SynthResult(status="skipped", reason=reason)


def price(model: str) -> tuple[float, float]:
    return PRICES.get(model, PRICES["claude-opus-5"])


def estimate_usd(model: str, prompt_chars: int, max_out_tokens: int) -> float:
    """Worst case for one call: the whole prompt in, and the model writing right up to max_tokens."""
    pin, pout = price(model)
    return (prompt_chars / CHARS_PER_TOKEN * pin + max_out_tokens * pout) / 1_000_000


def least_covered(tools: dict[str, dict], counts: Counter, how_many: int, exclude: set[str]) -> list[str]:
    names = [n for n in tools if n not in exclude]
    return sorted(names, key=lambda n: (counts.get(n, 0), n))[:how_many]


def schema_text(tools: dict[str, dict], names: list[str]) -> str:
    lines = []
    for n in names:
        lines.append(json.dumps({"tool": n, "description": tools[n]["description"][:300], "args": tools[n]["schema"]}))
    return "\n".join(lines)


def parse(text: str) -> list[Any]:
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return []
    try:
        found = json.loads(match.group(0))
    except ValueError:
        return []
    return found if isinstance(found, list) else []


def validate(item: object, tools: dict[str, dict]) -> dict | None:
    """A clean synthetic example, or None when it does not fit the real schemas."""
    if not isinstance(item, dict) or not isinstance(item.get("request"), str):
        return None
    request = " ".join(item["request"].split())
    calls = item.get("calls")
    if not request or len(request) > 200 or looks_secret(request):
        return None
    if not isinstance(calls, list) or not 1 <= len(calls) <= 3:
        return None
    clean = []
    for call in calls:
        if schemas.validate_call(call, tools):
            return None
        clean.append({"tool": call["tool"], "args": call.get("args") or {}})
    if looks_secret([c["args"] for c in clean]):
        return None
    return {"source": "synthetic", "request": request, "calls": clean}


def generate(
    tools: dict[str, dict],
    counts: Counter,
    known_keys: set[str],
    *,
    client: Any = None,
    model: str,
    want: int,
    max_cost_usd: float,
    tools_per_night: int = 10,
    exclude: set[str] = frozenset(),
    stamp: str = "",
) -> SynthResult:
    if want <= 0:
        return skipped("synthetic_per_night is 0")
    if not tools:
        return skipped("no tool schemas found in data/nodes")
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return skipped("no ANTHROPIC_API_KEY")
        import anthropic

        client = anthropic.Anthropic()
    result = SynthResult()
    result.targets = least_covered(tools, counts, tools_per_night, set(exclude))
    if not result.targets:
        return skipped("no tools to target")
    prompt_base = schema_text(tools, result.targets)
    seen = set(known_keys)
    batches = 0
    while len(result.examples) < want and batches < 5:
        n = min(BATCH, want - len(result.examples))
        user = f"Tools:\n{prompt_base}\n\nWrite {n} examples spread over these tools, favouring the first ones."
        worst = estimate_usd(model, len(SYSTEM) + len(user), MAX_TOKENS)
        if result.cost_usd + worst > max_cost_usd:
            if batches == 0:
                return skipped(f"cost cap ${max_cost_usd:.2f} is below one batch (about ${worst:.3f})")
            result.reason = f"stopped at the ${max_cost_usd:.2f} cap"
            break
        response = client.messages.create(
            model=model, max_tokens=MAX_TOKENS, system=SYSTEM, messages=[{"role": "user", "content": user}]
        )
        batches += 1
        used_in = getattr(response.usage, "input_tokens", 0) or 0
        used_out = getattr(response.usage, "output_tokens", 0) or 0
        result.input_tokens += used_in
        result.output_tokens += used_out
        pin, pout = price(model)
        result.cost_usd += (used_in * pin + used_out * pout) / 1_000_000
        text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
        items = parse(text)
        before = len(result.examples)
        for item in items:
            result.generated += 1
            example = validate(item, tools)
            if example is None:
                result.invalid += 1
                continue
            k = common.key(example["request"])
            if k in seen:
                result.duplicate += 1
                continue
            seen.add(k)
            example["id"] = f"synth-{stamp}-{len(result.examples)}"
            result.examples.append(example)
            if len(result.examples) >= want:
                break
        if len(result.examples) == before:
            break  # a batch that added nothing: asking again would only spend money
    return result
