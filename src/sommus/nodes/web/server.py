"""Web node: search the web through a small, cheap worker model.

The server-side search tool, attached to every request, measured 2,440 tokens — about
30% of each command's prompt — for something used on a small fraction of commands.
As a node, its definition is ~80 tokens, and the bulky search results stay inside the
worker's own context: only a short answer comes back to the main conversation.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

WORKER_MODEL = os.environ.get("SOMMUS_SEARCH_MODEL", "claude-haiku-4-5")
PRICES = {"claude-haiku-4-5": (1.00, 5.00), "claude-sonnet-5": (2.00, 10.00)}  # $ per million in/out
SEARCH_USD = 0.01
READ = ToolAnnotations(read_only_hint=True)

INSTRUCTIONS = (
    "Search the web and answer the question. Be brief and concrete: the facts, numbers or times "
    "asked for, then the names of the sources in one short line. No preamble."
)

server = MCPServer(
    "sommus-web", instructions="Answers questions that need current information from the web.", log_level="WARNING"
)


def tool(annotations: ToolAnnotations) -> Callable:
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # never let a tool fail with no explanation
                raise ToolError(f"{type(e).__name__}: {e}") from e

        server.tool(annotations=annotations, structured_output=False)(wrapper)
        return fn

    return decorator


def cost_of(usage) -> float:
    in_price, out_price = PRICES.get(WORKER_MODEL, PRICES["claude-sonnet-5"])
    searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
    return (usage.input_tokens * in_price + usage.output_tokens * out_price) / 1_000_000 + SEARCH_USD * searches


@tool(READ)
def web_search(question: str) -> str:
    """Look something up on the web: weather, news, prices, schedules, anything current or uncertain.

    Args:
        question: The full question, e.g. "weather in Waterloo tomorrow" or "when does UW reading week start 2026".
    """
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=WORKER_MODEL,
        max_tokens=1024,
        system=INSTRUCTIONS,
        # One search: each extra one pulls a fresh page of results into the worker's input.
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
        messages=[{"role": "user", "content": question}],
    )
    answer = "".join(block.text for block in response.content if block.type == "text").strip()
    # The brain reads this trailer to count the worker's spend in the command's cost, then strips it.
    return f"{answer or 'The search came back empty.'}\n[cost:{cost_of(response.usage):.5f}]"


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
