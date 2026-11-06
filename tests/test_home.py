"""Home node: the SwitchBot plug tools, against a fake cloud (no network, no credentials)."""

import base64
import hashlib
import hmac

import pytest
from mcp import Client

from sommus.nodes.home import switchbot
from sommus.nodes.home.server import server

FAN = {"deviceId": "AAA", "deviceName": "Fan", "deviceType": "Plug Mini (US)"}
LAMP = {"deviceId": "BBB", "deviceName": "Desk Lamp", "deviceType": "Plug Mini (US)"}
HUB = {"deviceId": "CCC", "deviceName": "Hub Mini", "deviceType": "Hub Mini"}


class FakeCloud:
    def __init__(self, devices, power="off", watts=0):
        self.devices, self.power, self.watts, self.calls = devices, power, watts, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/devices":
            return {"deviceList": self.devices}
        if path.endswith("/status"):
            return {"power": self.power, "weight": self.watts}
        if path.endswith("/commands"):
            self.power = "on" if body["command"] == "turnOn" else "off"
            return {}
        raise AssertionError(path)


@pytest.fixture
def cloud(monkeypatch):
    fake = FakeCloud([HUB, FAN])
    monkeypatch.setattr(switchbot, "request", fake)
    return fake


def test_signature_matches_the_documented_recipe():
    h = switchbot.headers("tok", "sec", now_ms=1700000000000, nonce="n1")
    expected = base64.b64encode(hmac.new(b"sec", b"tok1700000000000n1", hashlib.sha256).digest()).decode()
    assert h["sign"] == expected and h["t"] == "1700000000000" and h["Authorization"] == "tok"


def test_missing_credentials_explain_the_setup(monkeypatch):
    monkeypatch.delenv("SWITCHBOT_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOT_SECRET", raising=False)
    with pytest.raises(switchbot.HomeError, match="Developer Options"):
        switchbot.credentials()


def test_one_plug_needs_no_name_and_the_hub_is_not_a_plug(cloud):
    assert switchbot.turn("", True) == "Fan told to turn on."
    method, path, body = cloud.calls[-1]
    assert (method, path, body["command"]) == ("POST", "/devices/AAA/commands", "turnOn")


def test_a_single_plug_answers_to_any_name(cloud):
    assert switchbot.turn("the ceiling fan", False) == "Fan told to turn off."


def test_two_plugs_are_told_apart_by_name(monkeypatch):
    fake = FakeCloud([FAN, LAMP])
    monkeypatch.setattr(switchbot, "request", fake)
    assert switchbot.turn("lamp", True) == "Desk Lamp told to turn on."
    assert fake.calls[-1][1] == "/devices/BBB/commands"
    with pytest.raises(switchbot.HomeError, match="Which one"):
        switchbot.turn("", True)
    with pytest.raises(switchbot.HomeError, match="No single plug matches"):
        switchbot.turn("toaster", True)


def test_status_reads_back_power_and_watts(cloud):
    cloud.power, cloud.watts = "on", 38.5
    assert switchbot.status("fan") == "Fan is on, drawing 38.5 W."
    cloud.power = "off"
    assert switchbot.status("fan") == "Fan is off."


def test_no_plug_on_the_account_says_so(monkeypatch):
    monkeypatch.setattr(switchbot, "request", FakeCloud([HUB]))
    with pytest.raises(switchbot.HomeError, match="No SwitchBot plug"):
        switchbot.turn("", True)


async def test_tools_have_the_right_tiers_and_errors_reach_the_model(cloud, monkeypatch):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["plug_status"].annotations.read_only_hint
        assert tools["plug_on"].annotations.destructive_hint is False
        ok = await client.call_tool("plug_on", {"plug": "fan"})
        assert ok.content[0].text == "Fan told to turn on."
        monkeypatch.setattr(switchbot, "request", FakeCloud([]))
        bad = await client.call_tool("plug_off", {})
    assert bad.is_error and "No SwitchBot plug" in bad.content[0].text
