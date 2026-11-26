"""Voice-ID database, in shadow mode: collect his voice now, decide about voice ID later.

Voice ID was built (speaker.py) and parked on 2026-10-06: a voiceprint made from the plain microphone scored his
voice differently through live mode's echo cancellation, and enrolling took too long. This collects what live mode
actually hears, a little every day, so a voiceprint can be built from real data instead of eight read-aloud lines.

It changes nothing about security. The PIN still guards every personal action; `data/voiceprint.json` is never
created or read here, so nothing is vouched for by voice. Everything stays on this Mac in `data/` (gitignored).

What is stored, per sentence said inside an open live conversation (after the wake word):
  - the sentence's 256-number voice embedding (not audio), its length, loudness and time;
  - a label for how sure we are it is him (below), and its score against the parked print (for the report only);
  - a short WAV clip, but only for the surest labels and only up to a cap, so a better model can be tried later
    without asking him to record again. `keep_clips = false` in config.toml turns clips off.

Labels, most to least sure:
  pin_said  the sentence in which he said his PIN out loud and it was accepted
  pin       a sentence said while the PIN unlock was still active
  live      any other sentence in an open conversation (almost always him, but a roommate or a video can slip in)

`sommus voice-db` reports whether the data is good enough: most of what he says forms one tight cluster (his
voice), the PIN-confirmed sentences should land inside it, and the stragglers are other voices or noise.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from sommus.interfaces.speaker import SAMPLE_RATE, Voiceprint

MODEL = "wespeaker-resnet34-LM"
TRUSTED = ("pin_said", "pin")  # labels that are certainly or almost certainly him
MIN_RMS = 0.004  # quieter than this is silence or a far-off murmur: not worth keeping
MAX_SECONDS = 12.0  # keep the last 12 s of a long sentence
CANDIDATE_FILE = "voiceprint.candidate.json"  # never read by Sommus: renaming it to voiceprint.json is Jashan's call
PARKED_FILE = "voiceprint.off.json"

READY_SAMPLES = 200  # sentences before the numbers mean much
READY_DAYS = 5  # on different days, rooms and moods
READY_TRUSTED = 10  # PIN-confirmed sentences to check the cluster against

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    day TEXT NOT NULL,
    seconds REAL NOT NULL,
    rms REAL NOT NULL,
    label TEXT NOT NULL,
    source TEXT NOT NULL,
    model TEXT NOT NULL,
    ref_score REAL,
    clip TEXT,
    embedding BLOB NOT NULL
)
"""


class VoiceDB:
    """data/voice_samples.db and data/voice_clips/."""

    def __init__(self, data_dir: Path | str, keep_clips: bool = True, max_clips: int = 200):
        self.dir = Path(data_dir)
        self.path = self.dir / "voice_samples.db"
        self.clips = self.dir / "voice_clips"
        self.keep_clips, self.max_clips = keep_clips, max_clips
        self._lock = threading.Lock()
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(_SCHEMA)

    @contextlib.contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def clip_count(self) -> int:
        return len(list(self.clips.glob("*.wav"))) if self.clips.is_dir() else 0

    def _write_clip(self, audio: np.ndarray, stamp: datetime) -> str:
        self.clips.mkdir(parents=True, exist_ok=True)
        name = f"{stamp:%Y%m%d-%H%M%S-%f}.wav"
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
        with wave.open(str(self.clips / name), "wb") as clip:
            clip.setnchannels(1)
            clip.setsampwidth(2)
            clip.setframerate(SAMPLE_RATE)
            clip.writeframes(pcm.tobytes())
        return name

    def add(
        self,
        embedding: np.ndarray,
        seconds: float,
        rms: float,
        label: str,
        source: str = "live",
        ref_score: float | None = None,
        audio: np.ndarray | None = None,
    ) -> int:
        stamp = datetime.now()
        with self._lock:
            clip = None
            if audio is not None and self.keep_clips and label in TRUSTED and self.clip_count() < self.max_clips:
                clip = self._write_clip(audio, stamp)
            with self._connect() as db:
                cursor = db.execute(
                    "INSERT INTO samples (at, day, seconds, rms, label, source, model, ref_score, clip, embedding)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stamp.isoformat(timespec="seconds"),
                        stamp.date().isoformat(),
                        round(seconds, 2),
                        round(rms, 5),
                        label,
                        source,
                        MODEL,
                        None if ref_score is None else round(ref_score, 4),
                        clip,
                        np.asarray(embedding, dtype=np.float32).tobytes(),
                    ),
                )
                return int(cursor.lastrowid or 0)

    def load(self) -> tuple[list[dict], np.ndarray]:
        """(metadata of every sample, their embeddings as one matrix)."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, at, day, seconds, rms, label, source, ref_score, clip, embedding FROM samples ORDER BY id"
            ).fetchall()
        meta = [
            dict(id=r[0], at=r[1], day=r[2], seconds=r[3], rms=r[4], label=r[5], source=r[6], ref_score=r[7], clip=r[8])
            for r in rows
        ]
        matrix = np.stack([np.frombuffer(r[9], dtype=np.float32) for r in rows]) if rows else np.zeros((0, 256))
        return meta, matrix


def parked_print(data_dir: Path | str) -> Voiceprint | None:
    """The October voiceprint, used only to put a number on each sample for the report, never to decide anything."""
    path = Path(data_dir) / PARKED_FILE
    if not path.exists():
        return None
    with contextlib.suppress(Exception):
        raw = json.loads(path.read_text())
        return Voiceprint(np.asarray(raw["centre"], dtype=np.float32), float(raw.get("threshold", 0.5)))
    return None


@dataclass
class Collector:
    """Turns a finished sentence into a stored sample. `capture` blocks (~0.1 s for the model): run it in a thread."""

    db: VoiceDB
    reference: Voiceprint | None = None
    embed: Callable[[np.ndarray], np.ndarray | None] | None = None
    skipped: int = field(default=0)

    def capture(self, audio: np.ndarray, label: str, source: str = "live") -> int | None:
        from sommus.interfaces import speaker

        audio = np.ascontiguousarray(audio, dtype=np.float32)[-int(MAX_SECONDS * SAMPLE_RATE) :]
        rms = float(np.sqrt(np.mean(audio**2))) if len(audio) else 0.0
        embedding = (self.embed or speaker.embed)(audio) if rms >= MIN_RMS else None
        if embedding is None:  # too quiet, or too short to say anything about whose voice it is
            self.skipped += 1
            return None
        ref = self.reference.score(embedding) if self.reference is not None else None
        return self.db.add(embedding, len(audio) / SAMPLE_RATE, rms, label, source, ref, audio)


# -- the report


@dataclass
class Analysis:
    total: int = 0
    trusted: int = 0
    days: int = 0
    clips: int = 0
    centre: np.ndarray | None = None
    threshold: float = 0.0
    scores: np.ndarray = field(default_factory=lambda: np.zeros(0))
    trusted_scores: np.ndarray = field(default_factory=lambda: np.zeros(0))
    kept: int = 0
    per_day: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    ready: bool = False


def robust_centre(matrix: np.ndarray, rounds: int = 4, keep: float = 0.7) -> np.ndarray:
    """His voice as the middle of the biggest tight cluster: average, keep the closest 70 %, repeat. Roommates, videos
    and noise sit far from it and stop pulling the average."""
    unit = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9)
    centre = unit.mean(axis=0)
    centre /= np.linalg.norm(centre) or 1.0
    for _ in range(rounds):
        scores = unit @ centre
        closest = unit[scores >= np.quantile(scores, 1 - keep)]
        centre = closest.mean(axis=0)
        centre /= np.linalg.norm(centre) or 1.0
    return centre.astype(np.float32)


def analyse(db: VoiceDB) -> Analysis:
    meta, matrix = db.load()
    result = Analysis(total=len(meta), clips=db.clip_count())
    for row in meta:
        result.per_day[row["day"]] = result.per_day.get(row["day"], 0) + 1
    result.days = len(result.per_day)
    result.trusted = sum(row["label"] in TRUSTED for row in meta)
    if len(meta) < 8:
        result.notes.append(
            f"Only {len(meta)} sentences so far: nothing to measure yet. It fills in as he uses live mode."
        )
        return result
    unit = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9)
    result.centre = robust_centre(unit)
    result.scores = unit @ result.centre
    result.kept = int(np.sum(result.scores >= np.quantile(result.scores, 0.3)))
    tight = result.scores[result.scores >= np.quantile(result.scores, 0.3)]
    result.threshold = float(np.clip(tight.mean() - 2.5 * tight.std(), 0.35, 0.6))
    mask = np.array([row["label"] in TRUSTED for row in meta])
    result.trusted_scores = result.scores[mask]
    below = int(np.sum(result.scores < result.threshold))
    result.notes.append(
        f"{below} of {len(meta)} sentences ({100 * below / len(meta):.0f} %) fall outside his cluster: other voices, "
        "videos or noise, or his voice on a bad day."
    )
    if result.trusted >= 3:
        missed = int(np.sum(result.trusted_scores < result.threshold))
        result.notes.append(
            f"PIN-confirmed sentences: {result.trusted}; {missed} scored below the threshold "
            f"(lowest {result.trusted_scores.min():.2f}, average {result.trusted_scores.mean():.2f})."
        )
    result.ready = (
        len(meta) >= READY_SAMPLES
        and result.days >= READY_DAYS
        and result.trusted >= READY_TRUSTED
        and bool(np.all(result.trusted_scores >= result.threshold))
    )
    return result


def report(db: VoiceDB) -> str:
    a = analyse(db)
    lines = [
        "Voice-ID database (shadow mode: the PIN still guards everything)",
        f"  sentences: {a.total}   PIN-confirmed: {a.trusted}   days with data: {a.days}   clips kept: {a.clips}",
    ]
    if a.per_day:
        recent = sorted(a.per_day.items())[-7:]
        lines.append("  last days: " + ", ".join(f"{day[5:]}: {count}" for day, count in recent))
    if a.centre is not None:
        buckets = np.histogram(a.scores, bins=np.arange(-0.1, 1.01, 0.1))[0]
        lines.append("  score against his cluster (each bar is a 0.1 step from -0.1):")
        top = max(buckets.max(), 1)
        for i, count in enumerate(buckets):
            lines.append(f"    {-0.1 + 0.1 * i:+.1f}  {'#' * int(30 * count / top):<30} {count}")
        lines.append(f"  suggested threshold: {a.threshold:.2f}")
    lines += [f"  {note}" for note in a.notes]
    if a.ready:
        lines.append(
            "  READY: enough data and every PIN-confirmed sentence fits. `sommus voice-db build` makes a candidate."
        )
    else:
        need = []
        if a.total < READY_SAMPLES:
            need.append(f"{READY_SAMPLES - a.total} more sentences")
        if a.days < READY_DAYS:
            need.append(f"{READY_DAYS - a.days} more days")
        if a.trusted < READY_TRUSTED:
            need.append(
                f"{READY_TRUSTED - a.trusted} more PIN-confirmed sentences (say the PIN out loud instead of typing it)"
            )
        if need:
            lines.append("  Not ready yet. Still needs: " + "; ".join(need) + ".")
    return "\n".join(lines)


def build_candidate(db: VoiceDB) -> Path | None:
    """Write data/voiceprint.candidate.json from the data so far. Nothing reads that file: to try it for real,
    copy it to voiceprint.json (that is the step that would let his voice skip the PIN)."""
    a = analyse(db)
    if a.centre is None:
        return None
    path = db.dir / CANDIDATE_FILE
    path.write_text(json.dumps({"centre": a.centre.round(6).tolist(), "threshold": round(a.threshold, 3)}))
    return path
