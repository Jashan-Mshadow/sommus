"""The wake-word model's plumbing, with fake ONNX sessions: no download, no audio device."""

import numpy as np

from sommus.interfaces import wakeword


class Fake:
    def __init__(self, outputs):
        self.outputs = outputs

    def run(self, _, feeds):
        return [self.outputs(feeds)]


def detector(scores):
    """A WakeWord whose wake model returns the next score from `scores` for each 80 ms frame."""
    found = iter(scores)
    mel = Fake(lambda feeds: np.zeros((8, 1, 1, 32), dtype=np.float32))
    embed = Fake(lambda feeds: np.zeros((1, 1, 1, 96), dtype=np.float32))
    wake = Fake(lambda feeds: np.array([[next(found, 0.0)]], dtype=np.float32))
    w = wakeword.WakeWord(folder=None)
    w._sessions = (mel, embed, wake)
    w.reset()
    found = iter(scores)  # reset scored 4 s of noise; the test's scores start now
    return w


def test_any_chunk_size_is_scored_in_80_ms_frames():
    w = detector([0.1] * 10)
    assert not w.feed(np.zeros(512, dtype=np.float32))  # less than a frame: nothing scored yet
    assert not w.feed(np.zeros(wakeword.FRAME * 2, dtype=np.float32))
    assert len(w._pending) == 512


def test_one_phrase_wakes_once_not_once_per_frame():
    w = detector([0.9, 0.95, 0.9, 0.2] + [0.9] * 3)
    frame = np.zeros(wakeword.FRAME, dtype=np.float32)
    heard = [w.feed(frame) for _ in range(7)]
    assert heard == [True, False, False, False, False, False, False]  # the cooldown covers the whole phrase


def test_quiet_frames_never_wake_it():
    w = detector([0.1, 0.3, 0.49])
    assert not any(w.feed(np.zeros(wakeword.FRAME, dtype=np.float32)) for _ in range(3))
