"""Voice ID: is this Jashan talking?

Personal actions (email, messages, files) wait for his PIN, because anyone in the room — a roommate, a
video — can say "Hey Sommus, email my prof". With a voiceprint, a request said in his voice vouches for
itself and the PIN stays as the override. Everything runs on the Mac: a 26 MB WeSpeaker ResNet34 model
(CC BY 4.0, trained on VoxCeleb) on onnxruntime turns any utterance into a 256-number "voice embedding",
and two embeddings of the same person point the same way (cosine similarity near 1).

The voiceprint is a few embeddings of his voice (`sommus voiceprint`), kept in data/ (gitignored). No audio
is stored.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000
MODEL_REPO = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
MODEL_FILE = "voxceleb_resnet34_LM.onnx"
PRINT_FILE = "voiceprint.json"
MIN_SECONDS = 1.0  # shorter than this, an embedding says little: neither yes nor no
# Cosine similarity to the voiceprint. Same speaker with this model is typically 0.55–0.85, someone else
# under 0.3; between, it's unsure and nothing is vouched. Tuned from his own samples by `sommus voiceprint`.
DEFAULT_THRESHOLD = 0.5


@functools.cache
def _session():
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download

    try:
        path = hf_hub_download(MODEL_REPO, MODEL_FILE, local_files_only=True)
    except Exception:
        path = hf_hub_download(MODEL_REPO, MODEL_FILE)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    return ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])


def features(audio: np.ndarray) -> np.ndarray:
    """80 log-mel filterbanks every 10 ms, Kaldi-style as WeSpeaker was trained, mean removed per utterance."""
    import torch
    from torchaudio.compliance import kaldi

    wave = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32) * 32768.0).unsqueeze(0)
    fbank = kaldi.fbank(
        wave,
        num_mel_bins=80,
        frame_length=25,
        frame_shift=10,
        dither=0.0,
        sample_frequency=SAMPLE_RATE,
        window_type="hamming",
        energy_floor=0.0,
    )
    return (fbank - fbank.mean(dim=0)).numpy()


def embed(audio: np.ndarray) -> np.ndarray | None:
    """The utterance's voice embedding (unit length), or None when it's too short to say anything."""
    if len(audio) < MIN_SECONDS * SAMPLE_RATE:
        return None
    feats = features(audio)[None, :, :].astype(np.float32)
    vector = _session().run(["embs"], {"feats": feats})[0][0]
    return vector / (np.linalg.norm(vector) or 1.0)


@dataclass
class Voiceprint:
    centre: np.ndarray
    threshold: float = DEFAULT_THRESHOLD

    @classmethod
    def from_samples(cls, samples: list[np.ndarray]) -> Voiceprint:
        """Average of his embeddings; the threshold sits below his own spread (leave-one-out), never above 0.6."""
        stack = np.stack(samples)
        centre = stack.mean(axis=0)
        centre /= np.linalg.norm(centre) or 1.0
        if len(samples) >= 4:
            own = []
            for i in range(len(samples)):
                rest = np.delete(stack, i, axis=0).mean(axis=0)
                own.append(float(samples[i] @ (rest / (np.linalg.norm(rest) or 1.0))))
            threshold = float(np.clip(np.mean(own) - 2.5 * np.std(own), 0.35, 0.6))
        else:
            threshold = DEFAULT_THRESHOLD
        return cls(centre, threshold)

    def score(self, embedding: np.ndarray) -> float:
        return float(embedding @ self.centre)

    def save(self, data_dir: Path) -> Path:
        path = data_dir / PRINT_FILE
        path.write_text(json.dumps({"centre": self.centre.round(6).tolist(), "threshold": round(self.threshold, 3)}))
        return path

    @classmethod
    def load(cls, data_dir: Path) -> Voiceprint | None:
        path = data_dir / PRINT_FILE
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        return cls(np.asarray(raw["centre"], dtype=np.float32), float(raw.get("threshold", DEFAULT_THRESHOLD)))


def check(voiceprint: Voiceprint | None, audio: np.ndarray) -> tuple[bool | None, float | None]:
    """(is it him?, score). None when there's no voiceprint or the audio is too short to tell."""
    if voiceprint is None:
        return None, None
    embedding = embed(audio)
    if embedding is None:
        return None, None
    score = voiceprint.score(embedding)
    return score >= voiceprint.threshold, round(score, 3)


ENROL_LINES = [
    "Hey Sommus, what's my next class?",
    "Turn the volume down a bit and pause the music.",
    "Remind me to email Professor Davison tomorrow at nine.",
    "What's on my to-do list for today?",
    "Text Didi that I'm running about ten minutes late.",
    "Read me my unread emails, the important ones only.",
    "Set a timer for twenty-five minutes, I'm studying linear algebra.",
    "Hey Sommus, what's the weather like in Waterloo tomorrow morning?",
]
