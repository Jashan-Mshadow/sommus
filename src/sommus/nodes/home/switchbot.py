"""SwitchBot cloud API v1.1: signing, device lookup, plug commands.

Credentials come from .env: SWITCHBOT_TOKEN and SWITCHBOT_SECRET (SwitchBot app > Profile > Preferences, tap
App Version about ten times > Developer Options). The cloud API is what makes a plug controllable from anywhere:
the brain on the always-on server calls it, so Jashan needs neither to be on the plug's wifi nor to have his Mac on.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import uuid
from typing import Any

API = "https://api.switch-bot.com/v1.1"
SETUP = (
    "SwitchBot isn't set up. In the SwitchBot app: Profile > Preferences > tap App Version about ten times > "
    "Developer Options, copy the Token and Secret Key into SWITCHBOT_TOKEN and SWITCHBOT_SECRET in .env "
    "(on the server too), then restart."
)
PLUGS = ("Plug Mini (US)", "Plug Mini (JP)", "Plug Mini (EU)", "Plug", "Relay Switch 1", "Relay Switch 1PM")


class HomeError(Exception):
    """Something the model should hear about in plain words."""


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    return "" if value.startswith("${") else value  # an unset .env key reaches the node as the literal "${NAME}"


def credentials() -> tuple[str, str]:
    token, secret = _env("SWITCHBOT_TOKEN"), _env("SWITCHBOT_SECRET")
    if not token or not secret:
        raise HomeError(SETUP)
    return token, secret


def headers(token: str, secret: str, now_ms: int | None = None, nonce: str | None = None) -> dict[str, str]:
    """The v1.1 signature: base64(HMAC-SHA256(secret, token + timestamp_ms + nonce))."""
    t = str(now_ms if now_ms is not None else int(time.time() * 1000))
    nonce = nonce or str(uuid.uuid4())
    digest = hmac.new(secret.encode(), f"{token}{t}{nonce}".encode(), hashlib.sha256).digest()
    return {
        "Authorization": token,
        "sign": base64.b64encode(digest).decode(),
        "t": t,
        "nonce": nonce,
        "Content-Type": "application/json; charset=utf8",
    }


def request(method: str, path: str, body: dict | None = None) -> dict[str, Any]:
    """One call to the API. Returns the response's `body`; raises HomeError for anything but success."""
    import httpx2

    token, secret = credentials()
    try:
        response = httpx2.request(method, f"{API}{path}", headers=headers(token, secret), json=body, timeout=15)
    except httpx2.HTTPError as e:
        raise HomeError(f"Couldn't reach SwitchBot ({type(e).__name__}).") from e
    if response.status_code == 401:
        raise HomeError("SwitchBot refused the token or secret. Check SWITCHBOT_TOKEN and SWITCHBOT_SECRET.")
    if response.status_code == 429:
        raise HomeError("SwitchBot says too many requests today (10,000 a day).")
    try:
        data = response.json()
    except ValueError as e:
        raise HomeError(f"SwitchBot sent something unreadable (HTTP {response.status_code}).") from e
    if data.get("statusCode") != 100:
        raise HomeError(f"SwitchBot said no: {data.get('message') or data.get('statusCode')}.")
    return data.get("body") or {}


def devices() -> list[dict[str, Any]]:
    return request("GET", "/devices").get("deviceList", [])


def plugs() -> list[dict[str, Any]]:
    return [d for d in devices() if d.get("deviceType") in PLUGS]


def pick(name: str, available: list[dict[str, Any]]) -> dict[str, Any]:
    """The plug called `name` (as named in the SwitchBot app); one plug needs no name."""
    if not available:
        raise HomeError("No SwitchBot plug is on this account yet. Add it in the SwitchBot app first.")
    wanted = name.strip().casefold()
    if not wanted:
        if len(available) == 1:
            return available[0]
        raise HomeError("Which one? " + ", ".join(d["deviceName"] for d in available))
    exact = [d for d in available if d["deviceName"].casefold() == wanted]
    close = exact or [d for d in available if wanted in d["deviceName"].casefold()]
    if len(close) == 1:
        return close[0]
    if len(available) == 1 and not close:
        return available[0]  # "the fan" when the only plug is named "Plug Mini 3F2A": one plug can only mean it
    raise HomeError(
        f"No single plug matches '{name}'. The plugs are: " + ", ".join(d["deviceName"] for d in available) + "."
    )


def turn(name: str, on: bool) -> str:
    plug = pick(name, plugs())
    request("POST", f"/devices/{plug['deviceId']}/commands", {
        "command": "turnOn" if on else "turnOff", "parameter": "default", "commandType": "command",
    })  # fmt: skip
    return f"{plug['deviceName']} told to turn {'on' if on else 'off'}."


def status(name: str) -> str:
    plug = pick(name, plugs())
    s = request("GET", f"/devices/{plug['deviceId']}/status")
    line = f"{plug['deviceName']} is {s.get('power', 'unknown')}"
    watts = s.get("weight")  # the Plug Mini reports watts as "weight"
    if watts is not None and str(s.get("power", "")).lower() == "on":
        line += f", drawing {watts} W"
    return line + "."
