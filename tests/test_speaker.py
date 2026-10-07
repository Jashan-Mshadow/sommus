"""Voice ID: the voiceprint maths and what a match is allowed to unlock. No model, no audio."""

import numpy as np

from sommus.brain.pin import Gate
from sommus.interfaces import speaker


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_a_voiceprint_accepts_his_voice_and_rejects_another(tmp_path):
    rng = np.random.default_rng(7)
    him = unit(rng.normal(size=256))
    samples = [unit(him + 0.3 * unit(rng.normal(size=256))) for _ in range(8)]
    voiceprint = speaker.Voiceprint.from_samples(samples)
    assert 0.35 <= voiceprint.threshold <= 0.6
    assert voiceprint.score(unit(him + 0.3 * unit(rng.normal(size=256)))) >= voiceprint.threshold
    assert voiceprint.score(unit(rng.normal(size=256))) < voiceprint.threshold  # a stranger is near 0
    voiceprint.save(tmp_path)
    loaded = speaker.Voiceprint.load(tmp_path)
    assert loaded is not None and abs(loaded.threshold - voiceprint.threshold) < 1e-3
    assert speaker.Voiceprint.load(tmp_path / "nowhere") is None


def test_too_short_to_tell_is_neither_yes_nor_no(tmp_path):
    voiceprint = speaker.Voiceprint(unit(np.ones(256)))
    assert speaker.check(voiceprint, np.zeros(8000, dtype=np.float32)) == (None, None)  # half a second
    assert speaker.check(None, np.zeros(32000, dtype=np.float32)) == (None, None)  # no voiceprint yet


def test_his_voice_vouches_for_one_request_only(tmp_path):
    now = [0.0]
    gate = Gate(tmp_path, {"send_email"}, unlock_minutes=10, clock=lambda: now[0])
    assert gate.needs_pin("send_email")
    gate.vouch()
    assert not gate.needs_pin("send_email") and not gate.unlocked  # vouched, not unlocked for 10 minutes
    now[0] += 120
    assert gate.needs_pin("send_email")  # the vouch ran out with the request
    gate.vouch()
    gate.unvouch()  # the next voice wasn't his
    assert gate.needs_pin("send_email")
