"""The agent loop: user text in, a stream of events out.

Interfaces (terminal now, voice later) consume these events and supply a
`confirm` callback. The brain never prints, and never knows how it's being
talked to.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anthropic

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
class Notice:
    text: str


@dataclass(frozen=True)
class TurnDone:
    usage: Usage
    cost_usd: float
    steps: int


Event = TextDelta | ToolStarted | ToolFinished | Notice | TurnDone
ConfirmFn = Callable[[str, dict[str, Any]], Awaitable[bool]]


class Brain:
    def __init__(self, cfg: Config, hub: NodeHub, store: Store, client: anthropic.AsyncAnthropic | None = None):
        self.cfg = cfg
        self.hub = hub
        self.store = store
        self.client = client or anthropic.AsyncAnthropic()
        self.system = system_prompt(cfg)
        self.messages: list[dict[str, Any]] = []

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
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            tools=[*self.hub.api_tools(), *([WEB_SEARCH_TOOL] if self.cfg.web_search else [])],
            messages=self.messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.cfg.effort},
        )
        if self.cfg.model in FALLBACK_MODELS:
            request |= dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        return request

    async def handle(self, text: str, confirm: ConfirmFn) -> AsyncIterator[Event]:
        turn_id = self.store.start_turn(text)
        history_len = len(self.messages)
        self.messages.append({"role": "user", "content": stamp(text)})
        usage, reply, status, steps = Usage(), [], "error", 0

        try:
            while True:
                if steps == self.cfg.max_steps:
                    status = "max_steps"
                    yield Notice(f"Stopped after {steps} steps without finishing.")
                    break
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
                    if block.type == "server_tool_use":  # ran on Anthropic's side; nothing to execute
                        yield ToolStarted(block.name, dict(block.input), None)

                if response.stop_reason == "tool_use":
                    results = []
                    for block in response.content:
                        if block.type != "tool_use":
                            continue
                        yield ToolStarted(block.name, block.input, self.hub.tier(block.name))
                        output, is_error, decision = await self._run_tool(turn_id, block.name, block.input, confirm)
                        yield ToolFinished(block.name, output, is_error, decision)
                        results.append(
                            {"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error}
                        )
                    # All results go back in one message, so parallel calls keep working.
                    self.messages.append({"role": "user", "content": results})
                    continue

                if response.stop_reason == "pause_turn":
                    continue

                if response.stop_reason == "refusal":
                    status = "refusal"
                    del self.messages[history_len:]  # a refused turn doesn't stay in context
                    yield Notice("Claude declined that request.")
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
        except anthropic.APIConnectionError:
            del self.messages[history_len:]
            yield Notice("Couldn't reach the Claude API. Check the internet connection.")
        except BaseException:  # Ctrl+C mid-turn: forget the half-finished turn
            del self.messages[history_len:]
            status = "interrupted"
            raise
        finally:
            self.store.finish_turn(turn_id, "".join(reply), status, self.cfg.model, usage)

        yield TurnDone(usage, usage.cost_usd(self.cfg.model), steps)

    async def _run_tool(
        self, turn_id: int, name: str, input: dict[str, Any], confirm: ConfirmFn
    ) -> tuple[str, bool, str]:
        tier = self.hub.tier(name)
        if tier is None:
            output, is_error, decision = f"Unknown tool '{name}'.", True, "unknown"
        elif tier is Tier.BLOCKED:
            output, is_error, decision = "This action is blocked by the permission policy.", True, "blocked"
        elif tier is Tier.DESTRUCTIVE and self.cfg.ask_before_destructive and not await confirm(name, input):
            output, is_error, decision = f"{self.cfg.user} declined this action.", True, "declined"
        else:
            output, is_error = await self.hub.call(name, input)
            decision = "ran"
        self.store.log_tool(turn_id, name, input, tier.value if tier else None, decision, is_error, output)
        return output, is_error, decision
