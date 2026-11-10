"""The SwitchBot Plug Mini over Bluetooth, from the Mac: for while the plug has no Wi-Fi it can join (eduroam
asks for a login a plug can't give). The same commands the SwitchBot app sends when it's near, no account,
no hub. Reaches ~10 m, so it works while the Mac is awake in his room. Once the plug is on home Wi-Fi with
SWITCHBOT_TOKEN set, the home node's cloud tools are used first and this is only the fallback.

Protocol (as in the open-source pySwitchbot): commands go to characteristic cba20002, the reply comes back on
cba20003 with byte 1 = 0x80 for on. The plug advertises its state in its manufacturer data (company 2409):
byte 7 = 0x80 when on, bytes 10-11 the power draw in tenths of a watt.

macOS lets a process use Bluetooth only if its app declares it: Terminal does (Sommus live runs there),
the Claude app doesn't (a Bluetooth call from there aborts the process).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass
from pathlib import Path

SERVICE_DATA = "0000fd3d-0000-1000-8000-00805f9b34fb"
WRITE = "cba20002-224d-11e6-9fb8-0002a5d5c51b"
NOTIFY = "cba20003-224d-11e6-9fb8-0002a5d5c51b"
COMPANY = 2409  # Woan Technology (SwitchBot)
PLUG_MODELS = {ord("g"), ord("j")}  # Plug Mini US, JP
COMMANDS = {True: bytes.fromhex("570f50010180"), False: bytes.fromhex("570f50010100")}
CACHE = Path(__file__).resolve().parents[4] / "data" / "bluetooth_plug.txt"  # its address, for reference


class PlugError(Exception):
    """Something to say in plain words."""


@dataclass(frozen=True)
class Seen:
    on: bool | None
    watts: float | None


def parse(service_data: dict[str, bytes], manufacturer: dict[int, bytes]) -> Seen | None:
    """A Plug Mini's advertisement, or None if this isn't one."""
    model = service_data.get(SERVICE_DATA)
    if not model or (model[0] & 0x7F) not in PLUG_MODELS:
        return None
    m = manufacturer.get(COMPANY) or b""
    on = bool(m[7] & 0x80) if len(m) > 7 else None
    watts = (((m[10] << 8) | m[11]) & 0x7FFF) / 10 if len(m) > 11 else None
    return Seen(on, watts)


def say(on: bool | None, watts: float | None = None) -> str:
    if on is None:
        return "The fan's plug answered, but didn't say whether it's on."
    if on:
        return "Fan's on" + (f", drawing {watts:g} W." if watts else ".")
    return "Fan's off."


async def _find(seconds: float = 10.0):
    from bleak import BleakScanner

    found: asyncio.Future = asyncio.get_running_loop().create_future()

    def seen(device, adv) -> None:
        state = parse(adv.service_data, adv.manufacturer_data)
        if state and not found.done():  # the first Plug Mini heard: he has one
            found.set_result((device, state))

    try:
        async with BleakScanner(detection_callback=seen):
            device, state = await asyncio.wait_for(found, seconds)
    except TimeoutError as e:
        raise PlugError(
            "I can't find the fan's plug over Bluetooth. Is it plugged in and within about 10 m of the Mac?"
        ) from e
    except Exception as e:  # Bluetooth off, or no permission
        raise PlugError(f"The Mac's Bluetooth isn't available ({type(e).__name__}: {e}).") from e
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(device.address)
    return device, state


async def _turn(on: bool, attempts: int = 2) -> str:
    """Connecting takes ~13 s on this Mac (measured 2026-11-09), so a 30 s limit and one retry."""
    from bleak import BleakClient

    problem: Exception | None = None
    for _ in range(attempts):
        device, _ = await _find()
        reply: asyncio.Future = asyncio.get_running_loop().create_future()

        def answered(_sender, data: bytearray, reply=reply) -> None:
            if not reply.done():
                reply.set_result(bytes(data))

        try:
            async with BleakClient(device, timeout=30) as client:
                await client.start_notify(NOTIFY, answered)
                await client.write_gatt_char(WRITE, COMMANDS[on], response=False)
                data = await asyncio.wait_for(reply, 6)
        except Exception as e:  # a timeout or a dropped link: scan and try once more
            problem = e
            continue
        now_on = len(data) > 1 and data[1] == 0x80
        if now_on != on:
            raise PlugError(f"The plug answered but is still {'on' if now_on else 'off'}.")
        return say(now_on)
    if isinstance(problem, TimeoutError):
        raise PlugError("The fan's plug didn't answer over Bluetooth. Is the SwitchBot app on your phone holding it?")
    raise PlugError(f"Couldn't talk to the fan's plug ({type(problem).__name__}: {problem}).")


async def _status() -> str:
    _, state = await _find()
    return say(state.on, state.watts)


def _run(coro) -> str:
    """Bleak is async and the MCP tools are sync: run it on a fresh loop in its own thread."""
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        try:
            return pool.submit(asyncio.run, coro).result(timeout=100)
        except concurrent.futures.TimeoutError as e:
            raise PlugError("Bluetooth took too long; the fan's plug may be out of range.") from e


def turn(on: bool) -> str:
    return _run(_turn(on))


def status() -> str:
    return _run(_status())
