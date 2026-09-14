from mcp import Client

from sommus.nodes.laptop import macos
from sommus.nodes.laptop.server import server

EXPECTED_TIERS = {
    "get_battery": "read",
    "get_volume": "read",
    "list_apps": "read",
    "set_volume": "reversible",
    "set_mute": "reversible",
    "open_app": "reversible",
    "open_url": "reversible",
    "media_control": "reversible",
    "notify": "reversible",
    "lock_screen": "reversible",
    "sleep_display": "reversible",
    "quit_app": "destructive",
    "sleep_computer": "destructive",
}


async def test_every_tool_declares_the_expected_tier():
    from sommus.brain.permissions import tier_from_annotations

    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    assert {t.name: tier_from_annotations(t.annotations).value for t in tools} == EXPECTED_TIERS
    assert all(t.description for t in tools)


async def test_action_errors_reach_the_model_as_readable_tool_errors():
    async with Client(server) as client:
        result = await client.call_tool("open_url", {"url": "file:///etc/passwd"})
    assert result.is_error
    assert "Only http:// and https://" in result.content[0].text


async def test_set_volume_reports_new_state(monkeypatch):
    scripts = []

    def fake_osascript(*lines, argv=()):
        scripts.append(lines)
        return "output volume:30, input volume:50, alert volume:100, output muted:false"

    monkeypatch.setattr(macos, "_osascript", fake_osascript)
    async with Client(server) as client:
        result = await client.call_tool("set_volume", {"level": 30})
    assert result.content[0].text == "Volume 30%."
    assert scripts[0] == ("set volume output volume 30 without output muted",)
