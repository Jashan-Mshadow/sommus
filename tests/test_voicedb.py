"""Voice-ID database (shadow mode): what is stored, what the report says, and that nothing unlocks."""

import wave

import numpy as np

from sommus import voicedb
from sommus.interfaces.speaker import SAMPLE_RATE


def voice(seed: int, spread: float = 0.05) -> np.ndarray:
    """A made-up 256-number embedding near one fixed 'speaker' direction (seed picks the speaker)."""
    rng = np.random.default_rng(seed)
    base = np.random.default_rng(1000 + seed).normal(size=256)
    vector = base / np.linalg.norm(base) + rng.normal(scale=spread, size=256) * 0.2
    return (vector / np.linalg.norm(vector)).astype(np.float32)


def speech(seconds: float = 2.0, level: float = 0.1) -> np.ndarray:
    return (np.random.default_rng(3).normal(size=int(seconds * SAMPLE_RATE)) * level).astype(np.float32)


def collector(tmp_path, embeds, **kwargs):
    queue = iter(embeds)
    db = voicedb.VoiceDB(tmp_path, **kwargs)
    return voicedb.Collector(db, embed=lambda audio: next(queue)), db


def test_a_sentence_is_stored_as_numbers_with_its_label(tmp_path):
    collect, db = collector(tmp_path, [voice(1)])
    assert collect.capture(speech(), "live") == 1
    meta, matrix = db.load()
    assert meta[0]["label"] == "live" and meta[0]["clip"] is None and matrix.shape == (1, 256)
    assert not list(tmp_path.glob("*.wav")) and not (tmp_path / "voice_clips").exists()  # live sentences: no audio


def test_only_the_surest_labels_keep_a_clip_and_only_up_to_the_cap(tmp_path):
    collect, db = collector(tmp_path, [voice(1)] * 4, max_clips=2)
    for label in ("pin_said", "pin", "pin_said", "live"):
        collect.capture(speech(), label)
    meta, _ = db.load()
    assert [row["clip"] is not None for row in meta] == [True, True, False, False]
    clip = tmp_path / "voice_clips" / meta[0]["clip"]
    with wave.open(str(clip)) as wav:
        assert wav.getframerate() == SAMPLE_RATE and wav.getnframes() == 2 * SAMPLE_RATE


def test_clips_can_be_turned_off(tmp_path):
    collect, db = collector(tmp_path, [voice(1)], keep_clips=False)
    collect.capture(speech(), "pin_said")
    assert db.load()[0][0]["clip"] is None


def test_silence_and_too_short_audio_are_skipped(tmp_path):
    collect, db = collector(tmp_path, [None])
    assert collect.capture(speech(level=0.0001), "live") is None  # too quiet: the model is never asked
    assert collect.capture(speech(0.3), "live") is None  # the model says it is too short
    assert db.load()[0] == [] and collect.skipped == 2


def test_the_parked_print_only_puts_a_number_on_the_sample(tmp_path):
    reference = voicedb.Voiceprint(voice(1), 0.5)
    queue = iter([voice(1), voice(2)])
    db = voicedb.VoiceDB(tmp_path)
    collect = voicedb.Collector(db, reference=reference, embed=lambda audio: next(queue))
    collect.capture(speech(), "live")
    collect.capture(speech(), "live")
    meta, _ = db.load()
    assert meta[0]["ref_score"] > 0.9 and meta[1]["ref_score"] < 0.3


def fill(
    db, own=60, others=10, trusted=12, days=("2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23", "2026-11-24")
):
    for i in range(own):
        label = "pin_said" if i < trusted else "live"
        db.add(voice(1), 2.0, 0.1, label)
    for i in range(others):
        db.add(voice(2 + i), 2.0, 0.1, "live")
    with db._connect() as conn:  # spread the rows over several days
        for row, day in zip(range(1, own + others + 1), days * 100, strict=False):
            conn.execute("UPDATE samples SET day = ? WHERE id = ?", (day, row))


def test_the_report_finds_his_cluster_and_the_strangers(tmp_path):
    db = voicedb.VoiceDB(tmp_path)
    fill(db)
    a = voicedb.analyse(db)
    assert a.total == 70 and a.trusted == 12 and a.days == 5
    assert a.trusted_scores.min() > 0.9  # the PIN-confirmed sentences sit in the cluster
    assert 8 <= int(np.sum(a.scores < a.threshold)) <= 12  # the ten other voices fall outside it
    assert "sentences: 70" in voicedb.report(db)


def test_ready_needs_enough_data_days_and_pin_confirmed_sentences(tmp_path):
    db = voicedb.VoiceDB(tmp_path)
    fill(db, own=30, others=3, trusted=2)
    a = voicedb.analyse(db)
    assert not a.ready and "more sentences" in voicedb.report(db) and "PIN-confirmed" in voicedb.report(db)
    big = voicedb.VoiceDB(tmp_path / "big")
    fill(big, own=230, others=20, trusted=15)
    assert voicedb.analyse(big).ready and "READY" in voicedb.report(big)


def test_a_candidate_print_is_written_but_never_voiceprint_json(tmp_path):
    db = voicedb.VoiceDB(tmp_path)
    assert voicedb.build_candidate(db) is None  # nothing collected yet
    fill(db)
    path = voicedb.build_candidate(db)
    assert path.name == "voiceprint.candidate.json" and path.exists()
    assert not (tmp_path / "voiceprint.json").exists()  # the file that would let his voice skip the PIN
    from sommus.interfaces.speaker import Voiceprint

    assert Voiceprint.load(tmp_path) is None  # so Sommus loads no voiceprint and still asks for the PIN


def test_a_few_sentences_say_nothing_yet(tmp_path):
    db = voicedb.VoiceDB(tmp_path)
    db.add(voice(1), 2.0, 0.1, "live")
    assert voicedb.analyse(db).centre is None and "nothing to measure" in voicedb.report(db)
