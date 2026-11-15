"""Tuya (Merkury) plugs: signing, plug lookup, commands, and the merged list with SwitchBot. No network."""

import hashlib
import hmac

import pytest

from sommus.nodes.home import plugs, switchbot, tuya

LAMP = {"id": "d1", "name": "Lamp", "category": "cz", "online": True}
HUB = {"id": "d2", "name": "Gateway", "category": "wg2", "online": True}


class FakeTuya:
    def __init__(self, devices, switch=False, power=None):
        self.devices, self.switch, self.power, self.calls = devices, switch, power, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.startswith("/v1.0/iot-01/associated-users/devices"):
            return {"devices": self.devices}
        if path.endswith("/status"):
            out = [{"code": "switch_1", "value": self.switch}]
            return out + ([{"code": "cur_power", "value": self.power}] if self.power is not None else [])
        if path.endswith("/commands"):
            self.switch = body["commands"][0]["value"]
            return True
        raise AssertionError(path)


@pytest.fixture
def cloud(monkeypatch):
    for key in ("SWITCHBOT_TOKEN", "SWITCHBOT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TUYA_ACCESS_ID", "id1")
    monkeypatch.setenv("TUYA_ACCESS_SECRET", "sec1")
    fake = FakeTuya([HUB, LAMP])
    monkeypatch.setattr(tuya, "request", fake)
    return fake


def test_signature_follows_the_documented_recipe():
    got = tuya.sign("id", "sec", "GET", "/v1.0/token?grant_type=1", b"", "1700000000000", "n1")
    sts = "GET\n" + hashlib.sha256(b"").hexdigest() + "\n\n/v1.0/token?grant_type=1"
    want = hmac.new(b"sec", ("id" + "" + "1700000000000" + "n1" + sts).encode(), hashlib.sha256).hexdigest().upper()
    assert got == want
    with_token = tuya.sign("id", "sec", "GET", "/x", b"", "1", "n", token="tok")
    assert with_token != tuya.sign("id", "sec", "GET", "/x", b"", "1", "n")


def test_missing_credentials_explain_the_setup(monkeypatch):
    monkeypatch.delenv("TUYA_ACCESS_ID", raising=False)
    monkeypatch.setenv("TUYA_ACCESS_SECRET", "${TUYA_ACCESS_SECRET}")
    with pytest.raises(switchbot.HomeError, match="iot.tuya.com"):
        tuya.credentials()


def test_region_picks_the_endpoint(monkeypatch):
    monkeypatch.setenv("TUYA_ACCESS_ID", "a")
    monkeypatch.setenv("TUYA_ACCESS_SECRET", "b")
    monkeypatch.setenv("TUYA_REGION", "us-east")
    assert tuya.credentials()[2] == "https://openapi-ueaz.tuyaus.com"
    monkeypatch.setenv("TUYA_REGION", "mars")
    with pytest.raises(switchbot.HomeError, match="TUYA_REGION"):
        tuya.credentials()


def test_only_sockets_count_as_plugs(cloud):
    assert [p["deviceName"] for p in tuya.plugs()] == ["Lamp"]


def test_turn_on_sends_the_switch_command_and_status_follows(cloud):
    assert plugs.turn("lamp", True) == "Lamp told to turn on."
    assert cloud.calls[-1][2] == {"commands": [{"code": "switch_1", "value": True}]}
    cloud.power = 123
    assert plugs.status("lamp") == "Lamp is on, drawing 12.3 W."
    plugs.turn("lamp", False)
    assert plugs.status("lamp") == "Lamp is off."


def test_an_offline_plug_is_explained(monkeypatch, cloud):
    cloud.devices = [{**LAMP, "online": False}]
    with pytest.raises(switchbot.HomeError, match="offline"):
        plugs.turn("lamp", True)


def test_no_plug_on_a_tuya_only_account_says_so(monkeypatch, cloud):
    cloud.devices = [HUB]
    with pytest.raises(switchbot.HomeError, match="Tuya Smart / Smart Life"):
        plugs.turn("", True)


def test_both_brands_share_one_list(monkeypatch, cloud):
    monkeypatch.setenv("SWITCHBOT_TOKEN", "t")
    monkeypatch.setenv("SWITCHBOT_SECRET", "s")
    fan = {"deviceId": "AAA", "deviceName": "Fan", "deviceType": "Plug Mini (US)"}

    def fake_switchbot(method, path, body=None):
        if path == "/devices":
            return {"deviceList": [fan]}
        if path.endswith("/status"):
            return {"power": "off", "weight": 0}
        return {}

    monkeypatch.setattr(switchbot, "request", fake_switchbot)
    assert sorted(plugs.names()) == ["Fan", "Lamp"]
    assert plugs.status("fan") == "Fan is off."
    assert plugs.turn("lamp", True) == "Lamp told to turn on."
