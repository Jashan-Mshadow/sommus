"""Voice interface: what gets spoken, and when listening stops."""

import asyncio
import threading

import numpy as np
import pytest

from sommus.interfaces import voice


def test_markdown_and_links_are_not_read_aloud():
    text = "**Done** — opened [the notes](https://learn.uwaterloo.ca/x) · see `config.toml`"
    assert voice.clean_for_speech(text) == "Done, opened the notes, see config.toml"


def test_bare_urls_become_a_link():
    assert voice.clean_for_speech("Saved to https://example.com/a/b?c=1.") == "Saved to a link"


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("It's 2:45 PM.", "It's 2 45 PM."),
        ("Class at 3:00 PM.", "Class at 3 PM."),
        ("It's 14°C and cloudy.", "It's 14 degrees and cloudy."),
        ("Low of -2°.", "Low of -2 degrees."),
    ],
)
def test_times_and_temperatures_read_naturally(written, spoken):
    assert voice.clean_for_speech(written) == spoken


class FakeVoice:
    """Records what would be said; `play` blocks until released, like real audio does."""

    def __init__(self, hold=False):
        self.synthesized: list[str] = []
        self.played: list[str] = []
        self.cut_off: list[str] = []
        self.hold = hold
        self.playing = threading.Event()

    def synthesize(self, sentence):
        self.synthesized.append(sentence)
        if sentence == "broken":
            raise RuntimeError("voice fell over")
        return sentence.upper()

    def play(self, clip, stop):
        self.playing.set()
        if self.hold and stop.wait(2):
            self.cut_off.append(clip)
            return
        self.played.append(clip)

    def rest(self, stop):
        self.played.append("<dropped>" if stop.is_set() else "<rest>")


async def test_replies_are_spoken_sentence_by_sentence():
    engine = FakeVoice()
    speaker = voice.Speaker(engine)
    for delta in ["Brightness is ", "at 40%. Volume", " is muted! Anything", " else"]:
        speaker.feed(delta)
    speaker.flush()
    await speaker.finished()
    await speaker.close()

    assert engine.played == ["BRIGHTNESS IS AT 40%.", "VOLUME IS MUTED!", "ANYTHING ELSE", "<rest>"]
    assert speaker.first_word_at is not None


async def test_interrupt_cuts_off_the_sentence_and_drops_the_rest():
    engine = FakeVoice(hold=True)
    speaker = voice.Speaker(engine)
    speaker.feed("First sentence. Second sentence. Third")
    await asyncio.to_thread(engine.playing.wait, 2)
    speaker.interrupt()
    await asyncio.wait_for(speaker.finished(), 2)

    assert engine.cut_off == ["FIRST SENTENCE."] and engine.played == ["<dropped>"]
    assert speaker.buffer == ""  # the unfinished "Third" isn't spoken later either

    engine.hold = False
    speaker.feed("Next reply.")
    speaker.flush()
    await asyncio.wait_for(speaker.finished(), 2)
    await speaker.close()
    assert engine.played == ["<dropped>", "<dropped>", "NEXT REPLY.", "<rest>"]


async def test_a_voice_error_is_reported_and_speech_carries_on():
    engine, errors = FakeVoice(), []
    speaker = voice.Speaker(engine, on_error=errors.append)
    speaker.feed("broken\nStill here.")
    speaker.flush()
    await asyncio.wait_for(speaker.finished(), 2)
    await speaker.close()

    assert [str(e) for e in errors] == ["voice fell over"] and engine.played == ["STILL HERE.", "<rest>"]


def test_voice_config_picks_the_engine():
    kokoro = voice.make_voice({"kokoro_voice": "bm_george", "speed": 1.1})
    assert isinstance(kokoro, voice.KokoroVoice) and (kokoro.voice, kokoro.speed) == ("bm_george", 1.1)
    say = voice.make_voice({"engine": "say", "say_voice": "Samantha (Enhanced)", "say_rate": 200})
    assert isinstance(say, voice.SayVoice) and (say.voice, say.rate) == ("Samantha (Enhanced)", 200)


@pytest.mark.parametrize("name", ["jf_alpha", "zm_yunxi", "heart", "af_heart; rm -rf"])
def test_only_english_kokoro_voices_are_accepted(name):
    with pytest.raises(ValueError, match="English"):
        voice.KokoroVoice(name)


def test_say_plays_the_sentence_and_stops_when_told(monkeypatch):
    commands = []

    class FakeProcess:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.killed = False

        def poll(self):
            return -9 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self):
            return -9

    monkeypatch.setattr(voice.subprocess, "Popen", FakeProcess)
    stop = threading.Event()
    stop.set()
    voice.SayVoice("Samantha", 190).play("Hello there.", stop)
    assert commands == [["say", "-v", "Samantha", "-r", "190", "Hello there."]]


def run_detector(levels, **kwargs):
    detector = voice.SilenceDetector(**kwargs)
    states = [detector.update(level) for level in levels]
    return states[-1], states


def frames(seconds):
    return int(seconds / voice.FRAME_SECONDS)


def test_listening_stops_after_a_pause_in_speech():
    quiet, loud = 0.002, 0.08
    levels = [quiet] * 10 + [loud] * frames(1.0) + [quiet] * frames(0.95)
    final, states = run_detector(levels, silence_seconds=0.9)
    assert final == "done" and "speaking" in states


def test_a_short_breath_does_not_end_the_sentence():
    quiet, loud = 0.002, 0.08
    levels = [quiet] * 10 + [loud] * frames(0.5) + [quiet] * frames(0.3) + [loud] * frames(0.5)
    final, _ = run_detector(levels, silence_seconds=0.9)
    assert final == "speaking"


def test_nobody_speaking_times_out():
    final, _ = run_detector([0.002] * (10 + frames(8.1)), wait_seconds=8.0)
    assert final == "timeout"


def test_a_noisy_room_raises_the_threshold():
    """Background noise at 0.03 must not count as speech once measured."""
    noisy = 0.03
    final, states = run_detector([noisy] * 10 + [noisy] * frames(2.0), wait_seconds=10)
    assert "speaking" not in states


@pytest.mark.parametrize("heard", ["Thank you.", "you", "", " Thanks for watching!"])
def test_whisper_hallucinations_on_silence_are_dropped(monkeypatch, heard):
    import sys
    import types

    fake = types.SimpleNamespace(transcribe=lambda *a, **k: {"text": heard})
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    assert voice.Transcriber("model").transcribe(np.zeros(100, dtype=np.float32)) == ""
