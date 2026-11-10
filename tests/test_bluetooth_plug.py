"""The fan's SwitchBot plug over Bluetooth: reading its advertisement (no radio in tests)."""

from sommus.nodes.laptop import bluetooth_plug as bp

SVC = {bp.SERVICE_DATA: bytes([ord("g") | 0x80, 0x00])}


def mfr(on: bool, tenths_w: int) -> dict[int, bytes]:
    return {bp.COMPANY: bytes([1, 2, 3, 4, 5, 6, 0, 0x80 if on else 0x00, 0, 60, tenths_w >> 8, tenths_w & 0xFF])}


def test_a_plug_that_is_on_says_its_power():
    assert bp.parse(SVC, mfr(True, 253)) == bp.Seen(True, 25.3)
    assert bp.say(True, 25.3) == "Fan's on, drawing 25.3 W."


def test_a_plug_that_is_off():
    seen = bp.parse(SVC, mfr(False, 0))
    assert seen == bp.Seen(False, 0.0) and bp.say(seen.on, seen.watts) == "Fan's off."


def test_other_switchbot_devices_are_ignored():
    assert bp.parse({bp.SERVICE_DATA: b"H\x00"}, mfr(True, 1)) is None  # a Bot, not a plug
    assert bp.parse({}, {}) is None


def test_the_on_and_off_commands():
    assert bp.COMMANDS[True].hex() == "570f50010180" and bp.COMMANDS[False].hex() == "570f50010100"
