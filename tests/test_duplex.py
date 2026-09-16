"""Barge-in audio plumbing that can be checked without sound: resampling, the stream shape, and
the listener's 'someone started talking' signal."""

import threading

import numpy as np
import pytest

from sommus.interfaces import duplex, voice


@pytest.mark.parametrize("rate", [44_100, 48_000, 16_000])
def test_engine_mic_audio_becomes_16k_chunks_the_detector_can_use(rate):
    resampler = duplex.MicResampler(rate)
    chunks = []
    for _ in range(round(rate * 2 / 1024)):  # ~2 s in the engine's 1024-frame buffers
        chunks += resampler.feed(np.zeros(1024, dtype=np.float32))
    assert all(len(c) == duplex.CHUNK and c.dtype == np.float32 for c in chunks)
    assert abs(len(chunks) * duplex.CHUNK / duplex.MIC_RATE - 2.0) < 0.1


def test_the_echo_cancelled_mic_reads_like_a_sounddevice_stream():
    audio = duplex.DuplexAudio()
    audio.running = lambda: True
    audio.mic.put(np.ones(duplex.CHUNK, dtype=np.float32))
    block, overflowed = duplex.DuplexStream(audio).read(duplex.CHUNK)
    assert block.shape == (duplex.CHUNK, 1) and not overflowed


def test_a_stopped_engine_is_reported_so_the_listener_reopens_it():
    audio = duplex.DuplexAudio()  # never started
    with pytest.raises(RuntimeError):
        duplex.DuplexStream(audio).read(duplex.CHUNK)


def test_talking_over_sommus_fires_once_per_utterance_and_not_for_a_click():
    pattern = [0.9] * 3 + [0.0] * 40 + [0.9] * 30 + [0.0] * 40  # a click, then real speech
    scores = iter(pattern)
    fired, delivered = [], []

    class Stream:
        reads = 0

        def read(self, n):
            Stream.reads += 1
            if Stream.reads >= len(pattern):
                listener._stopping.set()
            return np.zeros((n, 1), dtype=np.float32), False

    detector = voice.SpeechDetector(probability=lambda chunk: next(scores, 0.0))
    listener = voice.Listener(detector, deliver=delivered.append, deaf=lambda: False, on_speech=lambda: fired.append(1))
    listener._listen(Stream(), voice.SAMPLE_RATE)
    assert len(fired) == 1 and len(delivered) == 1


def test_kokoro_plays_through_the_echo_cancelling_engine_when_barge_in_is_on():
    played = []

    class Output:
        def play(self, audio, stop):
            played.append(len(audio))

    kokoro = voice.KokoroVoice()
    kokoro.output = Output()
    kokoro.play(np.zeros(2400, dtype=np.float32), threading.Event())
    kokoro.rest(threading.Event())  # nothing to close: the engine stays up for the mic
    assert played == [2400]
