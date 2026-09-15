"""Fixed commands: what must match, and — more important — what must not."""

import pytest
from fakes import FakeModel, build_node, config, text_reply
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from sommus.brain import fastpath
from sommus.brain.loop import Brain, TextDelta, TurnDone
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store


@pytest.mark.parametrize(
    ("text", "tool", "args"),
    [
        ("brightness 20", "set_brightness", {"level": 20}),
        ("Turn my laptop brightness to 20", "set_brightness", {"level": 20}),
        ("set brightness to 75%", "set_brightness", {"level": 75}),
        ("please set the screen brightness to 40!", "set_brightness", {"level": 40}),
        ("volume 30", "set_volume", {"level": 30}),
        ("set the volume to 0", "set_volume", {"level": 0}),
        ("mute", "set_mute", {"muted": True}),
        ("unmute the sound", "set_mute", {"muted": False}),
        ("pause", "media_control", {"action": "play_pause"}),
        ("pause the music", "media_control", {"action": "play_pause"}),
        ("skip this song", "media_control", {"action": "next"}),
        ("lock my laptop", "lock_screen", {}),
        ("turn off the screen", "sleep_display", {}),
        ("what's my battery", "get_battery", {}),
        ("battery?", "get_battery", {}),
    ],
)
def test_simple_commands_skip_the_model(text, tool, args):
    assert fastpath.match(text) == fastpath.Match(tool, args)


@pytest.mark.parametrize(
    "text",
    [
        "make it a bit brighter",  # relative amounts need judgement
        "brightness 150",  # out of range
        "volume up",
        "set brightness to 20 and open spotify",  # compound
        "why is my battery draining so fast",  # a question, not a command
        "pause for a second and tell me the time",
        "don't mute",
        "lock the door",  # not a device we have
        "turn off my laptop",  # sleep_computer is destructive — leave it to the model
    ],
)
def test_anything_less_exact_goes_to_the_model(text):
    assert fastpath.match(text) is None


def device_node():
    calls = []
    node = MCPServer("device", log_level="WARNING")

    @node.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False), structured_output=False)
    def set_brightness(level: int) -> str:
        """Set brightness."""
        calls.append(level)
        return f"Brightness {level}%."

    return node, calls


async def test_a_fast_command_never_calls_the_model(tmp_path):
    node, calls = device_node()
    async with NodeHub((), Policy()) as hub:
        await hub.add("device", Client(node))
        model = FakeModel()  # no scripted responses: any API call would fail the test
        brain = Brain(config(tmp_path), hub, Store(tmp_path / "t.db"), client=model)

        events = [e async for e in brain.handle("brightness 20", lambda *_: True)]

        assert calls == [20] and model.requests == []
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["Brightness 20%."]
        assert isinstance(events[-1], TurnDone) and events[-1].cost_usd == 0
        assert brain.messages[-1] == {"role": "assistant", "content": "Brightness 20%."}  # follow-ups have context


async def test_a_fast_pattern_for_a_missing_tool_falls_through_to_the_model(tmp_path):
    node, _ = build_node()  # has no set_brightness
    async with NodeHub((), Policy()) as hub:
        await hub.add("test", Client(node))
        model = FakeModel(text_reply("I can't change brightness here."))
        brain = Brain(config(tmp_path), hub, Store(tmp_path / "t.db"), client=model)

        [e async for e in brain.handle("brightness 20", lambda *_: True)]

        assert len(model.requests) == 1
