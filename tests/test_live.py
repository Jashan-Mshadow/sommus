"""Live mode against a fake Gemini session: audio, interruptions, tools, the PIN, and the log."""

from types import SimpleNamespace

import numpy as np
import pytest
from fakes import FakeModel, build_node, config, text_reply
from mcp import Client

from sommus.brain import pin
from sommus.brain.loop import Brain
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store
from sommus.interfaces import live


class FakeSession:
    def __init__(self):
        self.sent_text, self.sent_audio, self.responses = [], [], []

    async def send_realtime_input(self, audio=None, text=None, **kwargs):
        if text is not None:
            self.sent_text.append(text)
        if audio is not None:
            self.sent_audio.append(audio)

    async def send_tool_response(self, function_responses):
        self.responses.extend(function_responses)

    async def close(self):
        pass


class FakePlayer:
    def __init__(self):
        self.played, self.flushed = [], 0

    def enqueue(self, audio):
        self.played.append(audio)

    def flush(self):
        self.flushed += 1


def message(**content):
    return SimpleNamespace(
        server_content=SimpleNamespace(
            **{
                "interrupted": None,
                "input_transcription": None,
                "output_transcription": None,
                "model_turn": None,
                "turn_complete": None,
                **content,
            }
        ),
        tool_call=None,
        tool_call_cancellation=None,
        session_resumption_update=None,
        go_away=None,
    )


def tool_message(name, args, id="call_1"):
    msg = message()
    msg.server_content = None
    msg.tool_call = SimpleNamespace(function_calls=[SimpleNamespace(id=id, name=name, args=args)])
    return msg


async def conversation(tmp_path, *replies, pin_tools=()):
    from dataclasses import replace

    node, calls = build_node()
    hub = NodeHub((), Policy())
    await hub.__aenter__()
    await hub.add("test", Client(node))
    cfg = replace(config(tmp_path, ask=False), pin_tools=pin_tools)
    brain = Brain(cfg, hub, Store(tmp_path / "t.db"), client=FakeModel(*replies), voice=True)
    shown, logged, pins = [], [], []
    conv = live.LiveConversation(
        FakeSession(), brain, FakePlayer(), shown.append, lambda **k: logged.append(k), lambda: pins.append(1)
    )
    return conv, hub, calls, shown, logged, pins


def test_audio_goes_both_ways_as_16_bit_pcm():
    audio = np.array([0.0, 0.5, -0.5, 1.0], dtype=np.float32)
    back = live.from_pcm16(live.to_pcm16([audio]))
    assert np.allclose(back, audio, atol=1e-4)


def test_gemini_gets_the_quick_tools_and_ask_sommus_only():
    tools = [
        {"name": "set_volume", "description": "Set volume.", "input_schema": {"type": "object", "title": "x"}},
        {"name": "send_email", "description": "Send.", "input_schema": {"type": "object"}},
    ]
    names = [d["name"] for d in live.declarations(tools)]
    assert names == ["set_volume", "ask_sommus"]  # email is reached through the Claude brain, with its rules
    assert "title" not in live.declarations(tools)[0]["parameters_json_schema"]


async def test_speech_plays_and_talking_over_it_stops_it(tmp_path):
    conv, hub, *_ = await conversation(tmp_path)
    part = SimpleNamespace(inline_data=SimpleNamespace(data=live.to_pcm16([np.zeros(240, dtype=np.float32)])))
    await conv.handle_message(message(model_turn=SimpleNamespace(parts=[part])))
    await conv.handle_message(message(interrupted=True))
    assert len(conv.player.played) == 1 and conv.player.flushed == 1
    await hub.__aexit__(None, None, None)


async def test_a_finished_exchange_is_shown_logged_and_stored(tmp_path):
    conv, hub, _, shown, logged, _ = await conversation(tmp_path)
    await conv.handle_message(message(input_transcription=SimpleNamespace(text="how's it going")))
    await conv.handle_message(message(output_transcription=SimpleNamespace(text="Not bad.")))
    await conv.handle_message(message(turn_complete=True))

    assert any("how's it going" in line for line in shown) and any("Not bad." in line for line in shown)
    assert logged[0]["text"] == "how's it going" and logged[0]["said"] == "Not bad."
    rows = conv.brain.store._db.execute("SELECT user_text, model FROM turns").fetchall()
    assert rows == [("how's it going", "gemini-live")]
    await hub.__aexit__(None, None, None)


async def test_a_pin_said_to_gemini_is_hidden_on_screen(tmp_path):
    conv, hub, _, shown, logged, _ = await conversation(tmp_path)
    await conv.handle_message(message(input_transcription=SimpleNamespace(text="my pin is 2684")))
    await conv.handle_message(message(turn_complete=True))
    assert "2684" not in str(shown) and "2684" not in str(logged)
    await hub.__aexit__(None, None, None)


async def test_saying_thats_all_ends_the_conversation(tmp_path):
    conv, hub, *_ = await conversation(tmp_path)
    await conv.handle_message(message(input_transcription=SimpleNamespace(text="That's all.")))
    await conv.handle_message(message(turn_complete=True))
    assert conv.dismissed
    await hub.__aexit__(None, None, None)


async def test_a_direct_tool_runs_through_the_brain_and_answers_gemini(tmp_path):
    conv, hub, calls, *_ = await conversation(tmp_path)
    await conv.run_tool(SimpleNamespace(id="c1", name="peek", args={}))
    assert calls == ["peek"]
    assert conv.session.responses[0].response == {"result": "all quiet"} and conv.session.responses[0].id == "c1"
    await hub.__aexit__(None, None, None)


async def test_ask_sommus_hands_the_request_to_claude(tmp_path):
    conv, hub, *_ = await conversation(tmp_path, text_reply("Why did calculus break up? Too many limits."))
    await conv.run_tool(SimpleNamespace(id="c2", name="ask_sommus", args={"request": "tell me a calculus joke"}))
    assert conv.session.responses[0].response == {"result": "Why did calculus break up? Too many limits."}
    await hub.__aexit__(None, None, None)


async def test_a_locked_tool_switches_to_listening_for_the_pin_on_the_mac(tmp_path):
    """The PIN must never be said to Google: Gemini is told to ask for it, and the mic goes local."""
    pin.set_pin(tmp_path, "5173")
    conv, hub, calls, _, _, pins = await conversation(tmp_path, pin_tools=("peek",))
    conv.exchange.heard.append("check my stuff")
    await conv.run_tool(SimpleNamespace(id="c3", name="peek", args={}))

    assert calls == [] and pins == [1]
    assert conv.session.responses[0].response["result"].startswith("LOCKED")
    assert conv.brain.gate.pending == "check my stuff"  # replayed once the PIN is heard
    await hub.__aexit__(None, None, None)


async def test_a_tool_gemini_gave_up_on_gets_no_answer(tmp_path):
    conv, hub, calls, *_ = await conversation(tmp_path)
    conv.cancelled.add("c4")
    await conv.run_tool(SimpleNamespace(id="c4", name="peek", args={}))
    assert calls == ["peek"] and conv.session.responses == []
    await hub.__aexit__(None, None, None)


async def test_the_resume_handle_and_goodbye_are_remembered(tmp_path):
    conv, hub, *_ = await conversation(tmp_path)
    msg = message()
    msg.server_content = None
    msg.session_resumption_update = SimpleNamespace(new_handle="h-123")
    msg.go_away = SimpleNamespace(time_left="5s")
    await conv.handle_message(msg)
    assert conv.handle == "h-123" and conv.going_away
    await hub.__aexit__(None, None, None)


def test_the_key_comes_from_the_gemini_cli_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(live.Path, "home", lambda: tmp_path)
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini/.env").write_text("GEMINI_API_KEY=abc\n")
    assert live.gemini_key() == "abc"
    (tmp_path / ".gemini/.env").write_text("")
    with pytest.raises(RuntimeError, match="aistudio"):
        live.gemini_key()


def test_the_relay_brain_hands_back_plain_results(tmp_path):
    """Claude's jokes and PIN questions, relayed through Gemini, sounded wrong (voice.log, 2026-09-28)."""
    from sommus.brain.prompt import system_prompt

    relay = system_prompt(config(tmp_path, ask=False), relay=True)
    assert "Relay mode" in relay and "JARVIS" not in relay and "LOCKED" in relay
    assert "JARVIS" in system_prompt(config(tmp_path, ask=False), voice=True)


async def test_tools_are_checked_against_his_words_never_sommus_own(tmp_path):
    conv, hub, *_ = await conversation(tmp_path)
    await conv.handle_message(message(input_transcription=SimpleNamespace(text="turn it down a bit")))
    await conv.handle_message(message(turn_complete=True))  # the exchange ends before the tool runs
    conv.brain.messages = [{"role": "assistant", "content": "Tomorrow is purely classes."}]
    await conv.run_tool(SimpleNamespace(id="c5", name="peek", args={}))
    assert conv.brain._asked == "turn it down a bit"
    await hub.__aexit__(None, None, None)


def test_the_instructions_keep_the_sarcasm_and_cut_the_filler():
    """He likes the quips (2026-09-28); what grated was filler and Claude's jokes relayed second-hand."""
    butler = live.instructions("Sommus", "Jashan")
    assert "Alfred" in butler and "quip in most replies" in butler
    assert "Answer those directly" in butler and "anything else?" in butler and "don't reply at all" in butler
    assert "One sec" not in butler
    assert "teenager" in live.instructions("Sommus", "Jashan", persona="teen")
    assert "a pirate" in live.instructions("Sommus", "Jashan", persona="You are a pirate.")


def test_every_voice_has_a_description():
    assert len(live.VOICES) == 30 and all(live.VOICES.values())


def test_the_pin_unlock_is_written_into_claudes_conversation(tmp_path):
    brain = Brain.__new__(Brain)
    brain.messages = []
    brain.note_unlocked()
    assert "unlocked" in str(brain.messages)
