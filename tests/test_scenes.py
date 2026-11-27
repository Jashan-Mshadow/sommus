"""Scenes: one phrase, several devices. What claims a request, how steps run, and what the reply says."""

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest
from fakes import config
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from sommus import scenes
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store
from sommus.config import NodeConfig
from sommus.server import Phone, Server

BOOK = scenes.read_config()  # the defaults shipped in config.toml


def test_the_default_scenes_load_from_config():
    assert set(BOOK) == {"goodnight", "study", "leaving"}
    goodnight = BOOK["goodnight"]
    assert [s.tool for s in goodnight.steps] == ["plug_off", "set_quiet", "sleep_display", "phone_focus"]
    assert goodnight.steps[0].fallback == "ble_plug_off" and goodnight.steps[0].args == {"plug": "lamp"}
    assert [s.tool for s in BOOK["leaving"].steps] == ["plug_off", "lock_screen"]
    assert BOOK["study"].name == "study mode"


def test_steps_it_cant_use_are_dropped_not_guessed():
    book = scenes.load(
        {
            "loop": {"steps": [{"tool": "run_scene", "args": {"name": "loop"}}, {"tool": "lock_screen"}, "junk", {}]},
            "odd": "not a table",
        }
    )
    assert list(book) == ["loop"] and [s.tool for s in book["loop"].steps] == ["lock_screen"]


@pytest.mark.parametrize(
    "said, scene",
    [
        ("goodnight", "goodnight"),
        ("Good night", "goodnight"),
        ("goodnight Sommus", "goodnight"),
        ("start my goodnight scene", "goodnight"),
        ("bedtime", "goodnight"),
        ("run study mode", "study"),
        ("study mode", "study"),
        ("turn on study mode", "study"),
        ("can you put me in study mode please", "study"),
        ("time to study", "study"),
        ("I'm leaving", "leaving"),
        ("i'm leaving now", "leaving"),
        ("I'm heading out", "leaving"),
        ("run the leaving scene", "leaving"),
    ],
)
def test_a_scene_phrase_said_on_its_own_runs_the_scene(said, scene):
    found = scenes.fast_match(said, BOOK)
    assert found is not None and found.tool == "run_scene" and found.args == {"name": scene}


@pytest.mark.parametrize(
    "said",
    [
        "goodnight, what's the weather tomorrow",
        "I'm leaving at 5",
        "I'm leaving, remind me to buy milk",
        "is it time to study",
        "turn off study mode",
        "what does study mode do",
        "I need to study",
        "study for the physics final",
        "set a goodnight alarm",
        "turn off the lamp",
        "lock",
        "good morning",
        "",
    ],
)
def test_anything_more_than_a_scene_phrase_goes_elsewhere(said):
    found = scenes.fast_match(said, BOOK)
    assert found is None or found.tool != "run_scene"


def test_no_scene_phrase_takes_over_an_everyday_command():
    """Scenes are matched before the rest of the fast path: "lock in" (filler "in" dropped) became "lock" and would
    have run study mode instead of locking the screen."""
    from sommus.brain import fastpath

    for scene in BOOK.values():
        for phrase in (scene.key, scene.name, *scene.phrases):
            core = " ".join(t for t in fastpath.words(phrase) if t not in scenes.FILLER)
            assert fastpath.match(core) is None, (phrase, core)


@pytest.mark.parametrize("said", ["what scenes do I have", "list my scenes", "which scenes are there", "show scenes"])
def test_asking_for_the_scenes_lists_them(said):
    found = scenes.fast_match(said, BOOK)
    assert found is not None and found.tool == "list_scenes"


@pytest.mark.parametrize("said", ["what does the goodnight scene do", "delete my scenes", "make a new scene"])
def test_other_scene_questions_go_to_the_model(said):
    assert scenes.fast_match(said, BOOK) is None


def test_with_no_scenes_nothing_is_claimed():
    assert scenes.fast_match("goodnight", {}) is None


def test_the_list_says_what_each_scene_does():
    said = scenes.say_list(BOOK)
    assert said.startswith("You have 3 scenes: goodnight (lamp off, Mac quiet, Mac screen off, Sleep Focus on)")
    assert "I'm leaving (lamp off, Mac locked)" in said
    assert "config.toml" in scenes.say_list({})


# ---------------------------------------------------------------- running a scene, with fake devices


def result(text="ok", is_error=False, decision="ran"):
    return SimpleNamespace(text=text, is_error=is_error, decision=decision)


ASLEEP = "lock_screen runs on Jashan's MacBook, which isn't reachable right now (asleep, lid closed or offline)."
DEVICES = {
    "plug_off": "home",
    "ble_plug_off": "mac",
    "set_quiet": "mac",
    "sleep_display": "mac",
    "lock_screen": "mac",
    "phone_focus": "phone",
    "read_email": "server",
}


def runner(book, answers=None, phone=False, pin=(), calls=None):
    """A Scenes whose tools answer from `answers` (tool -> result or callable) and log to `calls`."""
    calls = [] if calls is None else calls
    answers = answers or {}

    async def call(tool, args):
        calls.append((tool, args))
        answer = answers.get(tool, result())
        return await answer() if callable(answer) else answer

    run = scenes.Scenes(read=lambda: book)
    run.call = call
    run.device_of = lambda tool: DEVICES.get(tool, "server")
    run.available = lambda device: device != "phone" or phone
    run.needs_pin = lambda tool: tool in pin
    return run, calls


async def test_goodnight_reports_each_device_and_says_the_phone_only_acts_from_the_action_button():
    run, calls = runner(BOOK)
    said = await run.run("goodnight")
    assert said == (
        "Lamp off, Mac quiet, Mac screen off. Phone: skipped, it only acts while you're using the Action Button. "
        "Goodnight."
    )
    assert ("plug_off", {"plug": "lamp"}) in calls and "phone_focus" not in [c[0] for c in calls]


async def test_an_asleep_mac_skips_its_steps_once_and_the_lamp_still_goes_off():
    run, calls = runner(BOOK, {"lock_screen": result(ASLEEP, True)})
    said = await run.run("leaving")
    assert said == "Lamp off. Mac: couldn't reach it."
    book = {"bed": scenes.load({"bed": {"steps": [{"tool": "set_quiet"}, {"tool": "sleep_display"}]}})["bed"]}
    run, calls = runner(book, {"set_quiet": result(ASLEEP, True)})
    assert await run.run("bed") == "Mac: couldn't reach it."
    assert [c[0] for c in calls] == ["set_quiet"]  # the second Mac step didn't wait another 4 s


async def test_devices_run_at_the_same_time_and_one_devices_steps_in_order():
    started = asyncio.Event()
    order = []

    async def lamp():
        order.append("plug_off")
        started.set()
        await asyncio.sleep(0.05)
        return result()

    async def quiet():
        # Only reachable if the lamp's step is running alongside: one after the other, this would time out.
        await asyncio.wait_for(started.wait(), 1)
        order.append("set_quiet")
        return result()

    async def display():
        order.append("sleep_display")
        return result()

    run, _ = runner(BOOK, {"plug_off": lamp, "set_quiet": quiet, "sleep_display": display})
    await run.run("goodnight")
    assert order.index("set_quiet") < order.index("sleep_display")


async def test_the_cloud_plug_failing_falls_back_to_bluetooth():
    run, calls = runner(BOOK, {"plug_off": result("SwitchBot couldn't reach the plug", True)})
    said = await run.run("leaving")
    assert [c[0] for c in calls if "plug" in c[0]] == ["plug_off", "ble_plug_off"]
    assert said == "Lamp off, Mac locked."


async def test_a_step_that_needs_the_pin_is_skipped_and_named_never_run():
    book = scenes.load({"mail": {"steps": [{"tool": "read_email"}, {"tool": "lock_screen", "done": "Mac locked"}]}})
    run, calls = runner(book, pin={"read_email"})
    said = await run.run("mail")
    assert "read_email" not in [c[0] for c in calls]
    assert said == "Mac locked. Skipped read email: it needs your PIN (say it, then run the scene again)."


async def test_a_step_the_brain_declined_or_blocked_is_named():
    book = scenes.load({"s": {"steps": [{"tool": "sleep_display"}, {"tool": "plug_off", "label": "Lamp"}]}})
    answers = {"sleep_display": result("declined", True, "declined"), "plug_off": result("b", True, "blocked")}
    run, _ = runner(book, answers)
    said = await run.run("s")
    assert "Skipped sleep display: it asks first" in said and "Skipped plug off: blocked" in said


async def test_a_step_waiting_for_the_mac_says_so():
    book = scenes.load({"s": {"steps": [{"tool": "set_quiet", "done": "Mac quiet"}]}})
    run, _ = runner(book, {"set_quiet": result("Jashan's MacBook is asleep, so set_quiet is queued: it runs ...")})
    assert await run.run("s") == "Mac quiet once the Mac wakes."


async def test_from_the_phone_its_steps_are_queued():
    run, calls = runner(BOOK, phone=True)
    said = await run.run("goodnight")
    assert ("phone_focus", {"mode": "Sleep"}) in calls
    assert said == "Lamp off, Mac quiet, Mac screen off, Sleep Focus on. Goodnight."


async def test_an_old_shortcut_taking_one_action_is_said_plainly():
    book = scenes.load({"s": {"steps": [{"tool": "phone_focus", "done": "Sleep Focus on"}]}})
    refused = result("Not done: the phone can do one thing per request; already queued: flashlight_on.")
    run, _ = runner(book, {"phone_focus": refused}, phone=True)
    assert await run.run("s") == "Phone: your Shortcut takes one action per request, so Sleep Focus on was skipped."


async def test_an_any_step_uses_the_device_he_is_talking_from_then_whatever_is_there():
    book = scenes.load(
        {
            "music": {
                "steps": [
                    {
                        "any": [
                            {"tool": "phone_music", "args": {"what": "Lofi"}, "device": "phone"},
                            {"tool": "media_control", "args": {"action": "play"}, "device": "mac"},
                        ],
                        "done": "music on",
                    }
                ]
            }
        }
    )
    run, calls = runner(book, phone=True)
    run.here = lambda: "phone"
    assert await run.run("music") == "Music on." and calls == [("phone_music", {"what": "Lofi"})]
    run, calls = runner(book, phone=False)
    assert await run.run("music") == "Music on." and calls == [("media_control", {"action": "play"})]


async def test_unknown_scene_and_missing_tools():
    run, _ = runner(BOOK)
    assert (await run.run("party")).startswith("There's no scene called party. Scenes: goodnight, study mode")
    run.has_tool = lambda tool: tool != "sleep_display"
    assert "Mac: sleep display isn't available here." in await run.run("goodnight")
    assert "no device runner" in await scenes.Scenes(read=lambda: BOOK).run("goodnight")


# ---------------------------------------------------------------- end to end on the server, through the brain


def fake_devices():
    calls = []
    laptop = MCPServer("laptop", log_level="WARNING")
    home = MCPServer("home", log_level="WARNING")
    reversible = ToolAnnotations(read_only_hint=False, destructive_hint=False)

    @laptop.tool(annotations=reversible, structured_output=False)
    def set_quiet(on: bool) -> str:
        calls.append("set_quiet")
        return "Quiet mode on."

    @laptop.tool(annotations=reversible, structured_output=False)
    def sleep_display() -> str:
        calls.append("sleep_display")
        return "Display is off."

    @laptop.tool(annotations=reversible, structured_output=False)
    def lock_screen() -> str:
        calls.append("lock_screen")
        return "Screen locked."

    @laptop.tool(annotations=reversible, structured_output=False)
    def read_note(name: str = "") -> str:
        calls.append("read_note")
        return "a note"

    @home.tool(annotations=reversible, structured_output=False)
    def plug_off(plug: str = "") -> str:
        calls.append(f"plug_off {plug}")
        return "Lamp off."

    return laptop, home, calls


async def server_with_devices(tmp_path, monkeypatch, book=None, pin=()):
    book = BOOK if book is None else book
    monkeypatch.setattr(scenes, "read_config", lambda: book)
    cfg = dataclasses.replace(
        config(tmp_path, ask=False), nodes=(NodeConfig("laptop", "x", {}, "mac"),), pin_tools=tuple(pin)
    )
    phone = Phone()
    laptop, home, calls = fake_devices()
    hub = NodeHub((), Policy())
    await hub.__aenter__()
    await hub.add("laptop", Client(laptop))
    await hub.add("home", Client(home))
    await hub.add("phone", Client(phone.node()))
    book_runner = scenes.Scenes(read=lambda: book)
    await hub.add("scenes", Client(book_runner.node()))
    server = Server(cfg, hub, Store(tmp_path / "t.db"), phone, log=lambda _line: None, scene_book=book_runner)
    return server, hub, calls, phone


async def say(brain, text):
    async def allow(name, args):
        return True

    out = []
    async for event in brain.handle(text, allow):
        if hasattr(event, "text") and type(event).__name__ == "TextDelta":
            out.append(event.text)
    return "".join(out)


async def test_goodnight_on_telegram_runs_free_and_never_claims_the_phone_did_anything(tmp_path, monkeypatch):
    server, hub, calls, phone = await server_with_devices(tmp_path, monkeypatch)
    try:
        said = await say(server.brain, "goodnight")
    finally:
        await hub.__aexit__(None, None, None)
    assert sorted(calls) == ["plug_off lamp", "set_quiet", "sleep_display"]
    assert calls.index("set_quiet") < calls.index("sleep_display")
    assert said.startswith("Lamp off, Mac quiet, Mac screen off. Phone: skipped") and said.endswith("Goodnight.")
    assert phone.actions == []


async def test_goodnight_from_the_iphone_hands_the_shortcut_the_sleep_focus(tmp_path, monkeypatch):
    server, hub, calls, phone = await server_with_devices(tmp_path, monkeypatch)
    try:
        answer = await server.ask("goodnight", many=True)
    finally:
        await hub.__aexit__(None, None, None)
    assert answer["action"] == "focus_on" and answer["mode"] == "Sleep"
    assert answer["actions"] == [{"action": "focus_on", "mode": "Sleep"}]
    assert "Sleep Focus on" in answer["reply"] and "plug_off lamp" in calls


async def test_locking_the_mac_in_a_scene_locks_the_pin_too(tmp_path, monkeypatch):
    server, hub, calls, _ = await server_with_devices(tmp_path, monkeypatch)
    gate = server.brain.gate
    gate.unlocked_until = gate.clock() + 600
    try:
        said = await say(server.brain, "I'm leaving")
    finally:
        await hub.__aexit__(None, None, None)
    assert said == "Lamp off, Mac locked." and not gate.unlocked


async def test_a_scene_never_gets_past_the_pin(tmp_path, monkeypatch):
    book = scenes.load({"notes": {"steps": [{"tool": "read_note"}, {"tool": "lock_screen", "done": "Mac locked"}]}})
    server, hub, calls, _ = await server_with_devices(tmp_path, monkeypatch, book, pin=("read_note",))
    try:
        said = await say(server.brain, "run my notes scene")
    finally:
        await hub.__aexit__(None, None, None)
    assert "read_note" not in calls and "lock_screen" in calls
    assert "needs your PIN" in said and server.brain.gate.pending is None  # no surprise replay later


async def test_what_scenes_do_i_have_is_answered_free(tmp_path, monkeypatch):
    server, hub, _, _ = await server_with_devices(tmp_path, monkeypatch)
    try:
        said = await say(server.brain, "what scenes do I have")
    finally:
        await hub.__aexit__(None, None, None)
    assert said.startswith("You have 3 scenes")


async def test_the_model_can_run_a_scene_by_name_and_its_steps_are_logged(tmp_path, monkeypatch):
    from fakes import FakeModel, text_reply, tool_call

    server, hub, calls, _ = await server_with_devices(tmp_path, monkeypatch)
    server.brain.client = FakeModel(tool_call("run_scene", {"name": "I'm leaving"}), text_reply("Off you go."))
    try:
        said = await say(server.brain, "I'm off to class, sort the room out")
    finally:
        await hub.__aexit__(None, None, None)
    assert said == "Off you go." and "lock_screen" in calls
    logged = server.store._db.execute("SELECT tool FROM tool_calls ORDER BY id").fetchall()
    assert sorted(row[0] for row in logged) == ["lock_screen", "plug_off", "run_scene"]


def test_tool_errors_are_reworded_for_his_ears():
    assert scenes.fastpath_error(ToolError("Error executing tool x: boom").args[0]) == "That didn't work"
