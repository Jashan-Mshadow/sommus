"""The phone does more: an `actions` list for Shortcuts that loop (v2), new phone tools, and which device answers.

An older Shortcut must see exactly what it did before: one `action` with its keys at the top level.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeModel, config, text_reply, tool_call
from mcp import Client
from starlette.testclient import TestClient

from sommus import devices, phone_more, scenes
from sommus.brain import fastpath
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store
from sommus.server import Phone, Server, shortcut_version

ZONE = ZoneInfo("America/Toronto")
MORNING = datetime(2026, 10, 8, 9, 0, tzinfo=ZONE)


def text(result) -> str:
    return result.content[0].text


async def test_away_from_the_phone_nothing_is_queued_and_the_reply_says_it_didnt_happen():
    phone = Phone(ZONE, clock=lambda: MORNING)
    async with Client(phone.node()) as client:
        for tool, args in [
            ("phone_open", {"app": "Spotify"}),
            ("phone_timer", {"minutes": 5}),
            ("phone_volume", {"level_percent": 30}),
            ("phone_navigate", {"to": "E7"}),
            ("phone_flashlight", {"on": True}),
        ]:
            said = text(await client.call_tool(tool, args))
            assert said.startswith("Not done") and "iPhone" in said and "didn't happen" in said
    assert phone.action == {} and phone.actions == []


async def test_an_old_shortcut_still_gets_one_action_per_request():
    phone = Phone(ZONE)
    phone.present = True
    async with Client(phone.node()) as client:
        await client.call_tool("phone_flashlight", {"on": True})
        second = text(await client.call_tool("phone_timer", {"minutes": 1}))
    assert "one thing per request" in second and second.startswith("Not done")
    assert phone.action == {"action": "flashlight_on"} and phone.actions == [{"action": "flashlight_on"}]


async def test_a_v2_shortcut_gets_every_action_in_order_and_action_stays_the_first():
    phone = Phone(ZONE)
    phone.present, phone.many = True, True
    async with Client(phone.node()) as client:
        assert text(await client.call_tool("phone_timer", {"minutes": 10})) == "Timer on your phone for 10 minutes."
        await client.call_tool("phone_open", {"app": "Spotify"})
        await client.call_tool("phone_music", {"what": "Lofi"})
        await client.call_tool("phone_brightness", {"level_percent": 40})
        await client.call_tool("phone_navigate", {"to": "E7 University of Waterloo", "mode": "walking"})
        await client.call_tool("phone_run_shortcut", {"name": "Log water", "input": "500"})
        await client.call_tool("phone_open", {"url": "https://learn.uwaterloo.ca"})
    assert phone.action == {"action": "timer", "seconds": 600, "minutes": 10.0, "label": ""}
    assert [a["action"] for a in phone.actions] == [
        "timer", "open_app", "music", "brightness", "navigate", "shortcut", "open_url",
    ]  # fmt: skip
    assert phone.actions[3] == {"action": "brightness", "level": 40, "fraction": 0.4}
    assert phone.actions[4]["url"] == "https://maps.apple.com/?daddr=E7%20University%20of%20Waterloo&dirflg=w"
    for action in phone.actions:  # the Shortcut knows exactly this vocabulary
        assert set(action) - {"action"} <= set(phone_more.VOCABULARY[action["action"]])


async def test_bad_phone_requests_queue_nothing():
    phone = Phone(ZONE)
    phone.present, phone.many = True, True
    async with Client(phone.node()) as client:
        assert "between 1 second and 24 hours" in text(await client.call_tool("phone_timer", {"minutes": 0}))
        assert "between" in text(await client.call_tool("phone_timer", {"minutes": 2000}))
        assert "0 to 100" in text(await client.call_tool("phone_volume", {"level_percent": 140}))
        assert "isn't a link" in text(await client.call_tool("phone_open", {"url": "javascript:alert(1)"}))
        assert text(await client.call_tool("phone_open", {})) == "Which app?"
        assert "must be" in text(await client.call_tool("phone_navigate", {"to": "E7", "mode": "teleport"}))
        assert text(await client.call_tool("phone_run_shortcut", {"name": " "})) == "Which Shortcut?"
    assert phone.actions == []


async def test_a_runaway_turn_cant_queue_a_hundred_actions():
    phone = Phone(ZONE)
    phone.present, phone.many = True, True
    async with Client(phone.node()) as client:
        for _ in range(10):
            last = text(await client.call_tool("phone_flashlight", {"on": True}))
    assert len(phone.actions) == 8 and last.startswith("Not done")


@pytest.mark.parametrize("seconds, said", [(45, "45 seconds"), (90, "1 min 30 s"), (600, "10 minutes"),
                                           (5400, "90 minutes"), (7200, "2 hours"), (3600, "1 hour")])  # fmt: skip
def test_timer_lengths_are_said_plainly(seconds, said):
    assert phone_more.say_span(seconds) == said


@pytest.mark.parametrize("body, version", [({}, 1), ({"v": 2}, 2), ({"v": "2"}, 2), ({"v": 2.0}, 2), ({"v": "x"}, 1)])
def test_the_shortcut_version_is_read_leniently(body, version):
    assert shortcut_version(body) == version


async def test_ask_returns_actions_additively(tmp_path):
    phone = Phone(ZONE)
    async with NodeHub((), Policy()) as hub:
        await hub.add("phone", Client(phone.node()))
        server = Server(config(tmp_path), hub, Store(tmp_path / "t.db"), phone, log=lambda _line: None)
        server.voice_brain.client = FakeModel(
            tool_call("phone_flashlight", {"on": True}, id="a"),
            tool_call("phone_open", {"app": "Maps"}, id="b"),
            text_reply("Done both."),
            tool_call("phone_flashlight", {"on": True}, id="c"),
            tool_call("phone_open", {"app": "Maps"}, id="d"),
            text_reply("Light on."),
            text_reply("Sure."),
        )
        new = await server.ask("flashlight and open maps", many=True)
        old = await server.ask("flashlight and open maps")
        plain = await server.ask("thanks")
    assert new["action"] == "flashlight_on"
    assert new["actions"] == [{"action": "flashlight_on"}, {"action": "open_app", "app": "Maps"}]
    assert old["action"] == "flashlight_on" and old["actions"] == [{"action": "flashlight_on"}]
    assert plain["action"] == "" and plain["actions"] == [] and phone.many is False


class Everyone:
    def is_mine(self, address: str) -> bool:
        return True


async def test_goodnight_on_the_action_button_runs_the_scene_instead_of_ending(tmp_path, monkeypatch):
    async with NodeHub((), Policy()) as hub:
        server = Server(config(tmp_path), hub, Store(tmp_path / "t.db"), Phone(ZONE), log=lambda _line: None)
        server.tailnet = Everyone()
        asked = []

        async def ask(text, many=False):
            asked.append((text, many))
            return {"reply": "Lamp off. Goodnight.", "action": "", "actions": []}

        server.ask = ask
        server.scenes.read = lambda: scenes.read_config()
        with TestClient(server.app()) as client:
            night = client.post("/ask", json={"text": "goodnight", "v": 2}).json()
            thanks = client.post("/ask", json={"text": "thanks"}).json()
            server.scenes.read = lambda: {}
            plain = client.post("/ask", json={"text": "goodnight"}).json()
    assert night["reply"] == "Lamp off. Goodnight." and asked == [("goodnight", True)]
    assert thanks == {"reply": "Going to sleep.", "action": "sleep"}  # exactly as before
    assert plain["action"] == "sleep"  # no scene called that: still the old "I'm done"


# ---------------------------------------------------------------- which device answers


def available(*devices_up):
    return lambda device: device in devices_up


def test_from_the_iphone_music_and_timers_and_apps_go_to_the_phone():
    timer = fastpath.reminder("set a timer for 10 minutes")
    routed = devices.route(timer, "phone", available("phone", "mac", "server"))
    assert routed.tool == "phone_timer" and routed.args == {"seconds": 600} and routed.fallback is None
    pause = fastpath.match("pause the music")
    assert devices.route(pause, "phone", available("phone", "mac")).args == {"control": "pause"}
    skip = fastpath.match("next song")
    assert devices.route(skip, "phone", available("phone")).tool == "phone_music"
    opened = fastpath.Match("open_app", "open_app", {"name": "Spotify"})
    assert devices.route(opened, "phone", available("phone")).args == {"app": "Spotify"}


def test_from_telegram_or_the_mac_they_stay_on_the_mac_and_the_server():
    timer = fastpath.reminder("set a timer for 1.5 hours") or fastpath.reminder("set a timer for 90 minutes")
    assert devices.route(timer, "", available("mac", "server")).tool == "set_reminder"
    assert devices.route(timer, "mac", available("mac", "server")).tool == "set_reminder"
    pause = fastpath.match("pause")
    assert devices.route(pause, "mac", available("mac", "phone")).tool == "media_control"
    assert devices.route(pause, "", available("mac")).tool == "media_control"


def test_an_unreachable_mac_falls_back_to_the_phone_when_its_there_else_keeps_the_mac():
    pause = fastpath.match("pause")
    assert devices.route(pause, "mac", available("phone")).tool == "phone_music"
    assert devices.route(pause, "", available()).tool == "media_control"  # its own "couldn't reach the Mac"


def test_requests_only_one_device_can_do_are_left_alone():
    for said in ("lock", "mute", "turn off the lamp", "battery"):
        quick = fastpath.match(said)
        assert devices.route(quick, "phone", available("phone")) is quick


def test_device_names_and_order():
    assert devices.order("phone") == ["phone", "mac", "home", "server"]
    assert devices.order("") == ["mac", "home", "server", "phone"]
    assert devices.device_of("laptop", ["laptop", "claude"]) == "mac"
    assert devices.device_of("phone", []) == "phone" and devices.device_of("home", []) == "home"
    assert devices.device_of("reminders", ["laptop"]) == "server" and devices.device_of(None, []) == "server"


async def test_set_a_timer_on_the_action_button_is_a_phone_timer_for_free(tmp_path):
    from sommus.reminders import Reminders

    phone = Phone(ZONE)
    async with NodeHub((), Policy()) as hub:
        await hub.add("phone", Client(phone.node()))
        await hub.add("reminders", Client(Reminders(tmp_path / "r.json").node()))
        server = Server(config(tmp_path), hub, Store(tmp_path / "t.db"), phone, log=lambda _line: None)
        server.voice_brain.client = FakeModel()  # no model call allowed
        answer = await server.ask("set a timer for 10 minutes", many=True)
        server.brain.client = FakeModel()

        async def allow(name, args):
            return True

        telegram = "".join([e.text async for e in server.brain.handle("set a timer for 10 minutes", allow)
                            if type(e).__name__ == "TextDelta"])  # fmt: skip
    assert answer["actions"] == [{"action": "timer", "seconds": 600, "minutes": 10.0, "label": ""}]
    assert answer["reply"] == "Timer on your phone for 10 minutes."
    assert telegram.startswith("Timer set for 10 min")  # from Telegram: the server's reminder, as before
