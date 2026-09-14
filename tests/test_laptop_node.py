from mcp import Client

from sommus.nodes.laptop import macos
from sommus.nodes.laptop.server import server

EXPECTED_TIERS = {
    "get_battery": "read",
    "find_contact": "read",
    "list_contacts": "read",
    "read_pdf": "read",
    "save_browser_tab": "reversible",
    "run_shell": "destructive",
    "list_page_links": "read",
    "click_page_link": "reversible",
    "compose_email": "reversible",
    "click": "reversible",
    "wait": "read",
    "list_browser_tabs": "read",
    "read_browser_tab": "read",
    "focus_browser_tab": "reversible",
    "type_text": "reversible",
    "screenshot": "read",
    "download_url": "reversible",
    "send_message": "destructive",
    "get_brightness": "read",
    "set_brightness": "reversible",
    "get_wifi": "read",
    "set_wifi": "destructive",
    "press_keys": "reversible",
    "get_now_playing": "read",
    "list_shortcuts": "read",
    "run_shortcut": "reversible",
    "create_reminder": "reversible",
    "list_reminders": "read",
    "get_clipboard": "read",
    "set_clipboard": "reversible",
    "find_files": "read",
    "read_file": "read",
    "append_to_file": "reversible",
    "open_file": "reversible",
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


async def test_protected_apps_cannot_be_quit(monkeypatch):
    monkeypatch.setenv("SOMMUS_PROTECT_APPS", "Terminal,Claude")
    monkeypatch.setattr(macos, "running_apps", lambda: [macos.RunningApp("Terminal", 1), macos.RunningApp("Notes", 2)])
    async with Client(server) as client:
        result = await client.call_tool("quit_app", {"name": "Terminal"})
    assert result.is_error and "protected" in result.content[0].text


async def test_list_apps_marks_protected_apps(monkeypatch):
    monkeypatch.setenv("SOMMUS_PROTECT_APPS", "Terminal")
    monkeypatch.setattr(macos, "running_apps", lambda: [macos.RunningApp("Terminal", 1), macos.RunningApp("Notes", 2)])
    monkeypatch.setattr(macos, "frontmost_app", lambda: "Notes")
    async with Client(server) as client:
        result = await client.call_tool("list_apps", {})
    assert "Terminal (protected, can't be quit)" in result.content[0].text
    assert "Notes," in result.content[0].text or "Notes." in result.content[0].text


async def test_run_shell_returns_output_and_exit_code():
    async with Client(server) as client:
        ok = await client.call_tool("run_shell", {"command": "echo hello"})
        failed = await client.call_tool("run_shell", {"command": "exit 3"})
    assert ok.content[0].text.strip() == "hello"
    assert "Exit code 3" in failed.content[0].text


async def test_run_shell_refuses_sudo():
    async with Client(server) as client:
        result = await client.call_tool("run_shell", {"command": "sudo whoami"})
    assert result.is_error and "sudo" in result.content[0].text


async def test_run_shell_stops_a_hanging_command():
    async with Client(server) as client:
        result = await client.call_tool("run_shell", {"command": "sleep 5", "timeout": 0.5})
    assert result.is_error and "was stopped" in result.content[0].text
