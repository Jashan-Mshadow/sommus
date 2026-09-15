"""The agent loop: user text in, a stream of events out.

Interfaces (terminal now, voice later) consume these events and supply a
`confirm` callback. The brain never prints, and never knows how it's being
talked to.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anthropic

from sommus.brain import fastpath
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Tier
from sommus.brain.prompt import stamp, system_prompt
from sommus.brain.store import Store, Usage
from sommus.config import Config

# Models that accept the server-side refusal fallback.
FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}

# Runs on Anthropic's servers — there is no local function to implement, and results
# arrive inside the same response. The basic variant on purpose: the 2026 one adds
# server-side result filtering (and a code-execution harness) worth ~3,150 extra input
# tokens on *every* command, measured, which isn't worth it for a personal assistant.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
TOOL_SEARCH_TOOL = {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}
SILENT_SERVER_TOOLS = {"tool_search_tool_bm25"}  # plumbing, not worth showing the user

# An hour, not the default five minutes: commands from a phone arrive far apart, and every
# expired cache means rewriting the whole prefix at 1.25-2x the input price.
CACHE = {"type": "ephemeral", "ttl": "1h"}


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolStarted:
    name: str
    input: dict[str, Any]
    tier: Tier | None


@dataclass(frozen=True)
class ToolFinished:
    name: str
    output: str
    is_error: bool
    decision: str  # "ran" | "declined" | "blocked" | "unknown"


@dataclass(frozen=True)
class ToolResult:
    """Internal: what _run_tool produced (blocks go to the API, text to logs and screens)."""

    blocks: list[dict[str, Any]]
    text: str
    is_error: bool
    decision: str
    extra_usd: float = 0.0  # spend inside the tool itself, e.g. the web search worker


@dataclass(frozen=True)
class Notice:
    text: str


@dataclass(frozen=True)
class TurnDone:
    usage: Usage
    cost_usd: float
    steps: int


Event = TextDelta | ToolStarted | ToolFinished | Notice | TurnDone
ConfirmFn = Callable[[str, dict[str, Any]], Awaitable[bool]]


COST_TRAILER = re.compile(r"\n?\[cost:(\d+(?:\.\d+)?)\]\s*$")
OLD_RESULT_CHARS = 300  # a finished turn's tool output, trimmed to this in later requests


def split_cost(text: str) -> tuple[str, float]:
    """Strip a node's '[cost:0.0123]' trailer and return the amount it reports."""
    found = COST_TRAILER.search(text)
    return (text[: found.start()], float(found.group(1))) if found else (text, 0.0)


IMAGES_KEPT = 1  # screenshots are ~1,200 tokens each and pile up fast in a browser task


class Brain:
    def __init__(self, cfg: Config, hub: NodeHub, store: Store, client: anthropic.AsyncAnthropic | None = None):
        self.cfg = cfg
        self.hub = hub
        self.store = store
        self.client = client or anthropic.AsyncAnthropic()
        deferred = [t["name"] for t in hub.api_tools(cfg.core_tools) if t.get("defer_loading")]
        self.system = system_prompt(cfg, deferred)
        self.messages: list[dict[str, Any]] = []
        # The terminal and Telegram can share one brain; a turn from one waits for the other.
        self.lock = asyncio.Lock()

    def reset(self) -> None:
        self.messages = []

    def _request(self) -> dict[str, Any]:
        request: dict[str, Any] = dict(
            model=self.cfg.model,
            max_tokens=16000,
            # The breakpoint sits on the system prompt, the last *stable* thing in the
            # request (tools render before system). Auto top-level caching instead caches
            # the last block — the user's message — writing a fresh entry every turn, which
            # measured at ~85% of the cost per command.
            system=[{"type": "text", "text": self.system, "cache_control": CACHE}],
            tools=self._tools(),
            messages=self.messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.cfg.effort},
        )
        if self.cfg.model in FALLBACK_MODELS:
            request |= dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        return request

    async def handle(self, text: str, confirm: ConfirmFn) -> AsyncIterator[Event]:
        quick = fastpath.match(text) if self.cfg.fast_path else None
        if quick and self.hub.tier(quick.tool) is not None:
            async for event in self._fast(text, quick, confirm):
                yield event
            return
        turn_id = self.store.start_turn(text)
        self._trim_history()
        self._compact_finished_turns()
        history_len = len(self.messages)
        self.messages.append({"role": "user", "content": stamp(text)})
        usage, reply, status, steps = Usage(), [], "error", 0

        budget = self.cfg.max_steps
        try:
            while True:
                if steps == budget:
                    if budget >= self.cfg.max_steps_hard:
                        status = "max_steps"
                        yield Notice(
                            f"Stopped after {steps} steps. Ask me to keep going, or break the task into parts."
                        )
                        break
                    # It said it needs a few more; taking them beats abandoning the task.
                    budget = min(budget + 10, self.cfg.max_steps_hard)
                    yield Notice(f"Taking {budget - steps} more steps to finish.")
                steps += 1

                async with self.client.beta.messages.stream(**self._request()) as stream:
                    async for event in stream:
                        if event.type == "content_block_delta" and event.delta.type == "text_delta":
                            reply.append(event.delta.text)
                            yield TextDelta(event.delta.text)
                    response = await stream.get_final_message()

                usage.add(response.usage)
                self.messages.append({"role": "assistant", "content": response.content})

                for block in response.content:
                    if block.type == "server_tool_use" and block.name not in SILENT_SERVER_TOOLS:
                        yield ToolStarted(block.name, dict(block.input), None)  # ran server-side

                if response.stop_reason == "tool_use":
                    results = []
                    for block in response.content:
                        if block.type != "tool_use":
                            continue
                        yield ToolStarted(block.name, block.input, self.hub.tier(block.name))
                        result = await self._run_tool(turn_id, block.name, block.input, confirm)
                        usage.extra_usd += result.extra_usd
                        yield ToolFinished(block.name, result.text, result.is_error, result.decision)
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result.blocks,
                                "is_error": result.is_error,
                            }
                        )
                    remaining = budget - steps
                    if 0 < remaining <= 3:
                        # Told in-band, so it can wrap up deliberately instead of being cut off.
                        results.append(
                            {
                                "type": "text",
                                "text": f"[{self.cfg.name}: {remaining} steps left in this budget. Finish now, "
                                "or say plainly what you still need — more steps can be granted.]",
                            }
                        )
                    # All results go back in one message, so parallel calls keep working.
                    self.messages.append({"role": "user", "content": results})
                    self._prune_images()
                    self._mark_history_cache()
                    continue

                if response.stop_reason == "pause_turn":
                    continue

                if response.stop_reason == "refusal":
                    status = "refusal"
                    del self.messages[history_len:]  # a refused turn doesn't stay in context
                    yield Notice("The model declined that request.")
                elif response.stop_reason == "max_tokens":
                    status = "max_tokens"
                    yield Notice("The reply hit the length limit and was cut off.")
                else:
                    status = "ok"
                break

        except anthropic.AuthenticationError:
            del self.messages[history_len:]
            yield Notice("The API key was rejected. Check ANTHROPIC_API_KEY in .env.")
        except anthropic.RateLimitError:
            del self.messages[history_len:]
            yield Notice("Rate limited by the API. Wait a moment and try again.")
        except anthropic.APIStatusError as e:
            del self.messages[history_len:]
            yield Notice(f"API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError as e:
            del self.messages[history_len:]
            cause = e.__cause__ or e.__context__
            detail = f" ({type(cause).__name__}: {cause})" if cause else ""
            yield Notice(f"Couldn't reach the model API.{detail}")
        except BaseException:  # Ctrl+C mid-turn: forget the half-finished turn
            del self.messages[history_len:]
            status = "interrupted"
            raise
        finally:
            self.store.finish_turn(turn_id, "".join(reply), status, self.cfg.model, usage)

        yield TurnDone(usage, usage.cost_usd(self.cfg.model), steps)

    async def _fast(self, text: str, quick: fastpath.Match, confirm: ConfirmFn) -> AsyncIterator[Event]:
        """Run a fixed command straight against the node — no model call, no cost."""
        turn_id = self.store.start_turn(text)
        yield ToolStarted(quick.tool, quick.args, self.hub.tier(quick.tool))
        result = await self._run_tool(turn_id, quick.tool, quick.args, confirm)
        yield ToolFinished(quick.tool, result.text, result.is_error, result.decision)
        yield TextDelta(result.text)
        # Recorded as plain text so a follow-up ("a bit higher") still has the context.
        self.messages.append({"role": "user", "content": stamp(text)})
        self.messages.append({"role": "assistant", "content": result.text})
        self.store.finish_turn(turn_id, result.text, "ok" if not result.is_error else "error", "fastpath", Usage())
        yield TurnDone(Usage(), 0.0, 0)

    def _tools(self) -> list[dict[str, Any]]:
        tools = self.hub.api_tools(self.cfg.core_tools)
        if any(t.get("defer_loading") for t in tools):
            tools.insert(0, TOOL_SEARCH_TOOL)
        if self.cfg.web_search:
            tools.append(WEB_SEARCH_TOOL)
        return tools

    def _mark_history_cache(self) -> None:
        """Put one cache breakpoint on the newest tool results, and only there.

        Within a multi-step turn every step re-sends all the steps before it. With a
        breakpoint at the end, the next step reads them from cache at a tenth of the price.
        Older breakpoints are removed so the request never exceeds the limit of four.
        """
        for message in self.messages:
            if isinstance(message.get("content"), list):
                for block in message["content"]:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)
        last = self.messages[-1] if self.messages else None
        if last and last["role"] == "user" and isinstance(last["content"], list) and last["content"]:
            last["content"][-1]["cache_control"] = CACHE

    def _compact_finished_turns(self) -> None:
        """Shorten tool output from earlier turns before a new one starts.

        A contact list or a page of PDF text is needed while that command runs, and is dead
        weight after: one phone command measured 5,600 tokens of leftovers from earlier turns.
        Done once per turn boundary, so the conversation stays byte-stable within a turn.
        """
        for message in self.messages:
            if message["role"] != "user" or not isinstance(message["content"], list):
                continue
            for block in message["content"]:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                parts = block.get("content")
                if not isinstance(parts, list):
                    continue
                compacted = []
                for part in parts:
                    if part.get("type") == "text" and len(part["text"]) > OLD_RESULT_CHARS:
                        compacted.append({"type": "text", "text": part["text"][:OLD_RESULT_CHARS] + " …[trimmed]"})
                    elif part.get("type") == "image":
                        compacted.append({"type": "text", "text": "[screenshot from an earlier command]"})
                    else:
                        compacted.append(part)
                block["content"] = compacted

    def _trim_history(self) -> None:
        """Keep only the most recent turns; a turn starts at a plain-text user message."""
        starts = [i for i, m in enumerate(self.messages) if m["role"] == "user" and isinstance(m["content"], str)]
        keep = self.cfg.history_turns - 1  # the turn about to start takes the last slot
        if len(starts) > keep:
            del self.messages[: starts[-keep] if keep > 0 else len(self.messages)]

    def _prune_images(self) -> None:
        """Replace all but the newest screenshot with a placeholder.

        One browser task measured 180k input tokens in a single turn, almost all of it
        old screenshots being resent. Only the latest one is ever useful.
        """
        kept = 0
        for message in reversed(self.messages):
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                inner = block.get("content") if isinstance(block, dict) else None
                if not isinstance(inner, list):
                    continue
                for index, part in enumerate(inner):
                    if isinstance(part, dict) and part.get("type") == "image":
                        kept += 1
                        if kept > IMAGES_KEPT:
                            inner[index] = {"type": "text", "text": "[earlier screenshot dropped to save context]"}

    async def _run_tool(self, turn_id: int, name: str, input: dict[str, Any], confirm: ConfirmFn) -> ToolResult:
        tier = self.hub.tier(name)
        blocks: list[dict[str, Any]] = []
        if tier is None:
            output, is_error, decision = f"Unknown tool '{name}'.", True, "unknown"
        elif tier is Tier.BLOCKED:
            output, is_error, decision = "This action is blocked by the permission policy.", True, "blocked"
        elif (
            tier is Tier.ALWAYS_ASK or (tier is Tier.DESTRUCTIVE and self.cfg.ask_before_destructive)
        ) and not await confirm(name, input):
            output, is_error, decision = f"{self.cfg.user} declined this action.", True, "declined"
        else:
            result = await self.hub.call(name, input)
            blocks, output, is_error, decision = result.blocks, result.text, result.is_error, "ran"
        output, extra = split_cost(output)
        blocks = [
            {**b, "text": split_cost(b["text"])[0]} if b.get("type") == "text" else b
            for b in (blocks or [{"type": "text", "text": output}])
        ]
        self.store.log_tool(turn_id, name, input, tier.value if tier else None, decision, is_error, output)
        return ToolResult(blocks, output, is_error, decision, extra)
