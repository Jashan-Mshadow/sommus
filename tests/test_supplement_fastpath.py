"""'I took my creatine' reaches the vault tool with no model call and no PIN."""

from dataclasses import replace

from fakes import FakeModel, config
from mcp import Client
from mcp.server.mcpserver import MCPServer

from sommus.brain.loop import Brain, TextDelta
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store


async def full_permission(name, args):
    return True


async def test_a_supplement_report_is_logged_by_plain_code_without_the_model_or_a_pin(tmp_path):
    seen = []
    node = MCPServer("vault")

    @node.tool(structured_output=False)
    def log_supplements(items: str, scheduled: str = "") -> str:
        seen.append((items, scheduled))
        return f"Logged {items}."

    async with NodeHub((), Policy()) as hub:
        await hub.add("vault", Client(node))
        model = FakeModel()  # no scripted replies: any model call would fail the test
        cfg = replace(config(tmp_path, ask=False), pin_tools=("append_to_note", "ask_claude"))
        brain = Brain(cfg, hub, Store(tmp_path / "t.db"), client=model)
        events = [e async for e in brain.handle("I took my creatine", full_permission)]

    assert model.requests == []
    assert seen and seen[0][0] == "creatine" and "magnesium" in seen[0][1]
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Logged creatine."
