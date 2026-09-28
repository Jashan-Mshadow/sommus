"""The whole voice loop, headless: scripted utterances in, a fake model, a silent voice.

Name barge-in shipped without ever working because this loop had no test: it waited for each reply to
finish before reading the microphone again, so "Sommus, stop" was only heard after Sommus had stopped.
"""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
from fakes import FakeModel, config, text_reply

from sommus.brain import loop as brain_loop
from sommus.interfaces import addressee, cli, voice

STORY = " ".join(f"Sentence number {i} of a long story." for i in range(40))


class SilentVoice:
    """Each sentence 'plays' for 50 ms, and stops early when told."""

    label = "silent"

    def __init__(self):
        self.played = 0

    def warm_up(self):
        pass

    def synthesize(self, sentence):
        return sentence

    def play(self, clip, stop):
        if not stop.wait(0.05):
            self.played += 1

    def rest(self, stop):
        pass


def scripted_listener(script, finished):
    """Delivers each (seconds after start, text) as an utterance; the fake Whisper reads the text back."""

    class Listener:
        def __init__(self, detector, deliver, deaf, **kwargs):
            self.deliver = deliver
            self._thread = threading.Thread(target=self._run, daemon=True)

        def _run(self):
            start = time.monotonic()
            for at, text in script:
                time.sleep(max(0.0, start + at - time.monotonic()))
                SAID[id_of(text)] = text
                self.deliver(np.full(1600, id_of(text), dtype=np.float32))
            time.sleep(0.8)
            finished.set()

        def start(self):
            self._thread.start()

        def stop(self):
            pass

    return Listener


SAID: dict[float, str] = {}


def id_of(text: str) -> float:
    return float(abs(hash(text)) % 10_000 + 1)


class ScriptedWhisper:
    def __init__(self, model):
        pass

    def warm_up(self):
        pass

    def transcribe(self, audio, expecting=None):
        return SAID[float(audio[0])]


async def run_voice(monkeypatch, tmp_path, script, *replies):
    finished = threading.Event()
    model = FakeModel(*replies)
    engine = SilentVoice()
    cfg = config(tmp_path, ask=False)
    settings = {"smart_turn": False, "barge_in": False, "room_filter": True, "awake_seconds": 30}

    async def no_telegram(*args):
        return None, ""

    async def prompt_until_done(session):
        while not finished.is_set():
            await asyncio.sleep(0.05)
        raise EOFError

    async def warm(voice_module, settings):
        return engine

    async def for_sommus(*args):
        return True

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(cli.config, "load", lambda: cfg)
    monkeypatch.setattr(cli.config, "section", lambda name, path=None: settings if name == "voice" else {})
    monkeypatch.setattr(cli, "PromptSession", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(cli, "Brain", lambda c, h, s, voice=False: brain_loop.Brain(c, h, s, client=model, voice=voice))
    monkeypatch.setattr(cli, "_start_telegram", no_telegram)
    monkeypatch.setattr(cli, "_prompt_line", prompt_until_done)
    monkeypatch.setattr(cli, "_warm_voice", warm)
    monkeypatch.setattr(cli, "raise_mic_input", lambda: None)
    monkeypatch.setattr(voice, "Transcriber", ScriptedWhisper)
    monkeypatch.setattr(voice, "Listener", scripted_listener(script, finished))
    monkeypatch.setattr(voice, "chime", lambda path: None)
    monkeypatch.setattr(voice.SpeechDetector, "probability", lambda self, chunk: 0.0)
    monkeypatch.setattr(addressee, "said_to_sommus", for_sommus)

    await asyncio.wait_for(cli.voice_chat(), timeout=20)
    log = [json.loads(line) for line in (tmp_path / "voice.log").read_text().splitlines()]
    return model, engine, log


async def test_saying_its_name_stops_it_mid_reply_and_it_keeps_listening(monkeypatch, tmp_path):
    script = [
        (0.1, "Hey Sommus, tell me a long story."),
        (0.5, "the weather is nice today"),  # someone else, while it talks: ignored
        (0.8, "Sommus, stop."),
        (1.4, "and what time is it?"),  # still awake: a follow-up, no name needed
    ]
    model, engine, log = await run_voice(monkeypatch, tmp_path, script, text_reply(STORY), text_reply("It's noon."))

    assert engine.played < 20  # cut off well before the 40 sentences ended
    assert [entry["outcome"] for entry in log] == ["run", "ignored: Sommus talking, no name", "stop", "run"]
    assert len(model.requests) == 2  # "stop" never became a request (it used to pause the music)
    assert "what time is it" in str(model.requests[1]["messages"][-1])


async def test_a_new_request_over_a_reply_replaces_it(monkeypatch, tmp_path):
    script = [(0.1, "Hey Sommus, tell me a long story."), (0.7, "Sommus, what's the date?")]
    model, engine, log = await run_voice(monkeypatch, tmp_path, script, text_reply(STORY))

    assert engine.played < 20
    assert [entry["outcome"] for entry in log] == ["run", "run"]
    assert len(model.requests) == 1  # "what's the date" took the free path, no second model call
