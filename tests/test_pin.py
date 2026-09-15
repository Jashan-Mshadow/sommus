"""The PIN gate: personal tools wait for the PIN, and the PIN itself never leaves the brain."""

import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from fakes import FakeModel, build_node, config, text_reply, tool_call
from mcp import Client

from sommus.brain import pin
from sommus.brain.loop import Brain, TextDelta, ToolFinished
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store

PIN = "5173"  # a test PIN, not anyone's real one


@pytest.mark.parametrize(
    ("said", "digits"),
    [
        ("5173", "5173"),
        ("51 73", "5173"),
        ("five one seven three", "5173"),
        ("fifty one seventy three", "5173"),
        ("My PIN is 5173.", "5173"),
        ("Override, 5173", "5173"),
        ("oh four two nine", "0429"),
    ],
)
def test_a_pin_is_heard_however_it_is_said(said, digits):
    assert pin.spoken_digits(said) == digits


@pytest.mark.parametrize("said", ["volume 40", "what time is it", "call 5173", "12", "set a timer for 5 minutes"])
def test_ordinary_messages_are_not_pins(said):
    assert pin.spoken_digits(said) is None


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_pin_is_stored_only_as_a_salted_hash(tmp_path):
    pin.set_pin(tmp_path, PIN)
    stored = (tmp_path / pin.PIN_FILE).read_text()
    assert PIN not in stored and "salt" in stored
    assert (tmp_path / pin.PIN_FILE).stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        pin.set_pin(tmp_path, "12")


def test_unlocking_lasts_a_few_minutes_then_locks_again(tmp_path):
    pin.set_pin(tmp_path, PIN)
    clock = Clock()
    gate = pin.Gate(tmp_path, {"read_email"}, unlock_minutes=10, clock=clock)
    assert gate.needs_pin("read_email") and not gate.needs_pin("set_volume")
    assert gate.attempt(PIN) == "Unlocked for 10 minutes."
    assert not gate.needs_pin("read_email")
    clock.now += 601
    assert gate.needs_pin("read_email")


def test_three_wrong_tries_lock_it_out_even_for_the_right_pin(tmp_path):
    pin.set_pin(tmp_path, PIN)
    clock = Clock()
    gate = pin.Gate(tmp_path, {"read_email"}, clock=clock)
    wrong = "Wrong PIN — I heard 4 digits."
    assert [gate.attempt("0000") for _ in range(3)] == [wrong, wrong, f"{wrong} Locked for 5 minutes."]
    assert gate.attempt(PIN).startswith("Too many wrong tries")
    clock.now += 301
    assert gate.attempt(PIN) == "Unlocked for 10 minutes."


@asynccontextmanager
async def gated_brain(tmp_path, *responses, set_pin=True):
    if set_pin:
        pin.set_pin(tmp_path, PIN)
    node, calls = build_node()
    async with NodeHub((), Policy()) as hub:
        await hub.add("test", Client(node))
        model = FakeModel(*responses)
        cfg = replace(config(tmp_path, ask=False), pin_tools=("peek",))
        yield Brain(cfg, hub, Store(tmp_path / "test.db"), client=model), model, calls


async def full_permission(name, args):
    return True


async def run(brain, text):
    return [event async for event in brain.handle(text, full_permission)]


async def test_a_personal_tool_waits_for_the_pin_then_the_request_runs_by_itself(tmp_path):
    async with gated_brain(
        tmp_path,
        tool_call("peek", {}),
        text_reply("That needs your PIN."),
        tool_call("peek", {}, id="toolu_2"),
        text_reply("All quiet in there."),
    ) as (brain, model, calls):
        first = await run(brain, "read my email")
        locked = [e for e in first if isinstance(e, ToolFinished)][0]
        assert calls == [] and locked.is_error and "PIN" in locked.output

        second = await run(brain, "five one seven three")
        assert calls == ["peek"]
        spoken = "".join(e.text for e in second if isinstance(e, TextDelta))
        assert spoken == "Unlocked for 10 minutes. All quiet in there."

        # the PIN reached neither the model, the conversation, nor the log
        assert all(PIN not in str(request["messages"]) for request in model.requests)
        assert PIN not in str(brain.messages) and "five one seven three" not in str(brain.messages)
        with sqlite3.connect(tmp_path / "test.db") as db:
            logged = db.execute("SELECT user_text FROM turns").fetchall()
        assert ("[PIN entered]",) in logged and all("five" not in text for (text,) in logged)


async def test_a_wrong_pin_runs_nothing_and_costs_nothing(tmp_path):
    async with gated_brain(tmp_path, tool_call("peek", {}), text_reply("That needs your PIN.")) as (
        brain,
        model,
        calls,
    ):
        await run(brain, "read my email")
        events = await run(brain, "0000")
        assert calls == [] and len(model.requests) == 2
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["Wrong PIN — I heard 4 digits."]


async def test_basic_tools_never_ask(tmp_path):
    async with gated_brain(tmp_path, tool_call("wipe", {"target": "cache"}), text_reply("Wiped.")) as (brain, _, calls):
        await run(brain, "clear the cache")
        assert calls == ["wipe cache"]


async def test_without_a_pin_set_personal_tools_say_how_to_set_one(tmp_path):
    async with gated_brain(tmp_path, tool_call("peek", {}), text_reply("Set a PIN first."), set_pin=False) as (
        brain,
        _,
        calls,
    ):
        events = await run(brain, "read my email")
        locked = [e for e in events if isinstance(e, ToolFinished)][0]
        assert calls == [] and "sommus pin" in locked.output


@pytest.mark.parametrize(
    ("said", "given", "rest"),
    [
        ("The PIN is 5173. Send message to Didi.", "5173", "Send message to Didi"),
        ("override 5173 read my email", "5173", "read my email"),
        ("Send Didi a message, my pin is 5173", "5173", "Send Didi a message"),
        ("five one seven three", "5173", ""),
        ("send the message now", None, "send the message now"),
        ("what's the code for room 5353", None, "what's the code for room 5353"),
    ],
)
def test_a_pin_inside_a_request_is_split_out(said, given, rest):
    assert pin.split_pin(said) == (given, rest)


def test_pins_are_redacted_for_screens_and_logs():
    assert pin.redact("The PIN is 5173. Send message to Didi.") == "•••• Send message to Didi"
    assert pin.redact("5173") == "••••"
    assert pin.redact("volume 40") == "volume 40"


async def test_a_pin_said_with_the_request_unlocks_and_runs_it_in_one_go(tmp_path):
    async with gated_brain(tmp_path, tool_call("peek", {}), text_reply("Nothing new.")) as (brain, model, calls):
        events = await run(brain, "The PIN is 5173. Check my email.")
        assert calls == ["peek"]
        assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Unlocked for 10 minutes. Nothing new."
        assert PIN not in str(model.requests) and PIN not in str(brain.messages)
        assert "Check my email" in str(model.requests[0]["messages"][-1])


async def test_after_unlocking_the_model_knows_it_is_unlocked(tmp_path):
    """Found in real use: the PIN was kept out of the conversation, so the model kept asking for it."""
    async with gated_brain(tmp_path, text_reply("Sure.")) as (brain, model, _):
        await run(brain, "5173")
        await run(brain, "okay, send the message now")
        history = str(model.requests[0]["messages"])
        assert "unlocked" in history and PIN not in history


def test_a_pin_said_with_a_pause_in_it_is_put_back_together(tmp_path):
    """Found in use: the speech detector ended the sentence mid-number, so '268' and '4' arrived
    as separate messages and the PIN came back wrong."""
    pin.set_pin(tmp_path, PIN)
    clock = Clock()
    gate = pin.Gate(tmp_path, {"read_email"}, clock=clock)
    assert gate.attempt("51") == "" and gate.needs_pin("read_email")
    assert gate.attempt("73") == "Unlocked for 10 minutes."


def test_pieces_are_forgotten_after_a_while(tmp_path):
    pin.set_pin(tmp_path, PIN)
    clock = Clock()
    gate = pin.Gate(tmp_path, {"read_email"}, clock=clock)
    assert gate.attempt("51") == ""
    clock.now += pin.PARTIAL_SECONDS + 1
    assert gate.attempt("73") == ""  # too late to join: it's the start of a new one
    assert gate.needs_pin("read_email")


def test_a_wrong_pin_says_how_many_digits_it_heard(tmp_path):
    """So a misheard number is obvious instead of looking like the PIN changed."""
    pin.set_pin(tmp_path, PIN)
    gate = pin.Gate(tmp_path, {"read_email"})
    assert gate.attempt("2604") == "Wrong PIN — I heard 4 digits."


async def test_a_number_on_its_own_never_reaches_the_model_while_locked(tmp_path):
    async with gated_brain(tmp_path, text_reply("unused")) as (brain, model, _):
        events = await run(brain, "268")
        assert model.requests == [] and [e for e in events if isinstance(e, TextDelta)] == []
