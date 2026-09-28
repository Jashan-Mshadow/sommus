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
from fakes import FakeModel, FakeStream, config, text_reply

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


class SlowStream(FakeStream):
    def __init__(self, message, delay):
        super().__init__(message)
        self.delay = delay

    async def __aenter__(self):
        await asyncio.sleep(self.delay)
        return await super().__aenter__()


class SlowModel(FakeModel):
    """The first answer takes a moment, like a real one: time for him to finish his sentence."""

    def __init__(self, *responses, delay=0.0):
        super().__init__(*responses)
        self.delay = delay

    def _stream(self, **request):
        self.requests.append(request | {"messages": list(request["messages"])})
        delay, self.delay = self.delay, 0.0
        return SlowStream(self.responses.pop(0), delay)


async def run_voice(monkeypatch, tmp_path, script, *replies, delay=0.0):
    finished = threading.Event()
    model = SlowModel(*replies, delay=delay)
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


async def test_the_rest_of_a_request_said_while_it_works_is_joined_to_it(monkeypatch, tmp_path):
    """voice.log, 2026-09-28: 'What's the schedule looking like?' then 'For tomorrow.' a second later. The
    second half was dropped as room talk, and the first got 'Australia's schedule, or yours?'."""
    script = [(0.1, "Hey Sommus, what's the schedule looking like?"), (0.6, "For tomorrow.")]
    model, _, log = await run_voice(monkeypatch, tmp_path, script, text_reply(STORY), text_reply("Packed."), delay=1.5)

    assert [entry["outcome"] for entry in log] == ["run", "run: the rest of the request just made"]
    last = str(model.requests[-1]["messages"][-1])
    assert "schedule looking like? For tomorrow." in last


async def test_its_own_voice_coming_back_is_not_taken_as_the_rest(monkeypatch, tmp_path):
    script = [(0.1, "Hey Sommus, tell me a long story."), (0.6, "Sentence number 3 of a long story.")]
    model, _, log = await run_voice(monkeypatch, tmp_path, script, text_reply(STORY))

    assert [entry["outcome"] for entry in log] == ["run", "ignored: Sommus talking, no name"]
    assert len(model.requests) == 1


def test_echo_is_told_apart_from_new_words():
    from sommus.interfaces import voice as v

    said = "Tomorrow's packed: calculus at 9:30, then physics."
    assert v.echo_of("calculus at 9 30 then physics", said)
    assert not v.echo_of("For tomorrow.", "Checking.")
    assert not v.echo_of("the Netflix tab and the YouTube tab", said)
