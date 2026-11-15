"""Tuya cloud API: Merkury, Geeni, Smart Life and other Tuya-based smart plugs.

Credentials come from .env: TUYA_ACCESS_ID and TUYA_ACCESS_SECRET (a cloud project on iot.tuya.com whose Devices
tab has the Tuya Smart / Smart Life app account linked), and TUYA_REGION (us, us-east, eu, cn, in; default us).
Like the SwitchBot path this runs on the always-on server, so the plug needs the internet, not the Mac.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any

from sommus.nodes.home.switchbot import HomeError

ENDPOINTS = {
    "us": "https://openapi.tuyaus.com",  # Western America
    "us-east": "https://openapi-ueaz.tuyaus.com",  # Eastern America
    "eu": "https://openapi.tuyaeu.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}
SETUP = (
    "Tuya isn't set up. Create a cloud project at iot.tuya.com, link the Tuya Smart / Smart Life app account under "
    "Devices > Link Tuya App Account, then put the project's Access ID and Access Secret in TUYA_ACCESS_ID and "
    "TUYA_ACCESS_SECRET in .env (on the server too), then restart."
)
PLUG_CATEGORIES = ("cz", "pc")  # Tuya category codes: socket, power strip
ERRORS = {
    1004: "Tuya refused the signature: check TUYA_ACCESS_ID and TUYA_ACCESS_SECRET.",
    1106: "Tuya says the app account isn't linked to the cloud project (Devices > Link Tuya App Account).",
    1100: "Tuya says the Access ID doesn't exist. Check TUYA_ACCESS_ID.",
    2406: "Tuya's permission error: unlink the app account from the project and link it again.",
    28841002: "Tuya's data center for this project doesn't match the app account's region. Try TUYA_REGION=us-east.",
}
_EMPTY = hashlib.sha256(b"").hexdigest()
_token: dict[str, Any] = {"value": "", "expires": 0.0}


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    return "" if value.startswith("${") else value  # an unset .env key reaches the node as the literal "${NAME}"


def configured() -> bool:
    return bool(_env("TUYA_ACCESS_ID") and _env("TUYA_ACCESS_SECRET"))


def credentials() -> tuple[str, str, str]:
    client_id, secret = _env("TUYA_ACCESS_ID"), _env("TUYA_ACCESS_SECRET")
    if not client_id or not secret:
        raise HomeError(SETUP)
    region = (_env("TUYA_REGION") or "us").casefold()
    if region not in ENDPOINTS:
        raise HomeError(f"TUYA_REGION must be one of {', '.join(ENDPOINTS)}.")
    return client_id, secret, ENDPOINTS[region]


def sign(client_id: str, secret: str, method: str, path: str, body: bytes, t: str, nonce: str, token: str = "") -> str:
    """HMAC-SHA256(secret, client_id + access_token + t + nonce + stringToSign), upper-case hex.

    stringToSign = METHOD, SHA256(body), signed headers (none), then the path with its query, joined by newlines.
    The token is empty on the call that fetches the token.
    """
    content = hashlib.sha256(body).hexdigest() if body else _EMPTY
    string_to_sign = f"{method}\n{content}\n\n{path}"
    message = f"{client_id}{token}{t}{nonce}{string_to_sign}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest().upper()


def _call(method: str, path: str, body: dict | None, token: str) -> dict[str, Any]:
    import httpx2

    client_id, secret, base = credentials()
    raw = json.dumps(body).encode() if body is not None else b""
    t, nonce = str(int(time.time() * 1000)), str(uuid.uuid4())
    headers = {
        "client_id": client_id,
        "sign": sign(client_id, secret, method, path, raw, t, nonce, token),
        "t": t,
        "nonce": nonce,
        "sign_method": "HMAC-SHA256",
        "Content-Type": "application/json",
    }
    if token:
        headers["access_token"] = token
    try:
        response = httpx2.request(method, base + path, headers=headers, content=raw or None, timeout=15)
        data = response.json()
    except httpx2.HTTPError as e:
        raise HomeError(f"Couldn't reach Tuya ({type(e).__name__}).") from e
    except ValueError as e:
        raise HomeError("Tuya sent something unreadable.") from e
    if not data.get("success"):
        code = data.get("code")
        raise HomeError(ERRORS.get(code) or f"Tuya said no: {data.get('msg') or code}.")
    return data.get("result")


def access_token() -> str:
    if _token["value"] and time.time() < _token["expires"] - 60:
        return _token["value"]
    result = _call("GET", "/v1.0/token?grant_type=1", None, "")
    _token["value"] = result["access_token"]
    _token["expires"] = time.time() + int(result.get("expire_time", 7200))
    return _token["value"]


def request(method: str, path: str, body: dict | None = None) -> Any:
    """One call to the API, with a cached token. Returns the response's `result`."""
    return _call(method, path, body, access_token())


def plugs() -> list[dict[str, Any]]:
    """Plugs on the linked account, shaped like SwitchBot's: deviceId and deviceName."""
    result = request("GET", "/v1.0/iot-01/associated-users/devices")
    devices = result.get("devices", []) if isinstance(result, dict) else (result or [])
    return [
        {"deviceId": d["id"], "deviceName": d.get("name") or d["id"], "online": d.get("online", True)}
        for d in devices
        if d.get("category") in PLUG_CATEGORIES
    ]


def _switch_code(values: list[dict[str, Any]]) -> str:
    for item in values:
        if str(item.get("code", "")).startswith("switch") and isinstance(item.get("value"), bool):
            return item["code"]
    return "switch_1"


def turn_plug(plug: dict[str, Any], on: bool) -> str:
    if plug.get("online") is False:
        raise HomeError(f"{plug['deviceName']} is offline (no wifi). Check its power and its 2.4 GHz connection.")
    code = _switch_code(request("GET", f"/v1.0/devices/{plug['deviceId']}/status") or [])
    request("POST", f"/v1.0/devices/{plug['deviceId']}/commands", {"commands": [{"code": code, "value": on}]})
    return f"{plug['deviceName']} told to turn {'on' if on else 'off'}."


def status_plug(plug: dict[str, Any]) -> str:
    if plug.get("online") is False:
        return f"{plug['deviceName']} is offline."
    values = request("GET", f"/v1.0/devices/{plug['deviceId']}/status") or []
    by_code = {v.get("code"): v.get("value") for v in values}
    is_on = by_code.get(_switch_code(values))
    line = f"{plug['deviceName']} is {'on' if is_on else 'off'}"
    if is_on and by_code.get("cur_power") is not None:
        line += f", drawing {by_code['cur_power'] / 10:g} W"  # Tuya reports cur_power in tenths of a watt
    return line + "."
