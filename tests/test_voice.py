"""Voice interface: what gets spoken, and when listening stops."""

import numpy as np
import pytest

from sommus.interfaces import voice


def test_markdown_and_links_are_not_read_aloud():
    text = "**Done** — opened [the notes](https://learn.uwaterloo.ca/x) · see `config.toml`"
    assert voice.clean_for_speech(text) == "Done, opened the notes, see config.toml"


def test_bare_urls_become_a_link():
    assert voice.clean_for_speech("Saved to https://example.com/a/b?c=1.") == "Saved to a link"


async def test_replies_are_spoken_sentence_by_sentence(monkeypatch):
    spoken = []

    class FakeProcess:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        spoken.append(args[-1])
        return FakeProcess()

    monkeypatch.setattr(voice.asyncio, "create_subprocess_exec", fake_exec)
    speaker = voice.Speaker("Samantha")
    for delta in ["Brightness is ", "at 40%. Volume", " is muted! Anything", " else"]:
        speaker.feed(delta)
    speaker.flush()
    await speaker.finished()
    await speaker.close()

    assert spoken == ["Brightness is at 40%.", "Volume is muted!", "Anything else"]


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
