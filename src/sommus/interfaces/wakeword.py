"""The wake word from the sound itself: openWakeWord's models (Apache-2.0 code), run straight on onnxruntime.

Transcribing every sentence with Whisper and searching the text for "Sommus" missed him most of the time:
Whisper doesn't know the word ("so miss", "summers"), and 428 of 579 logged utterances were dropped as
"asleep, no wake phrase". A wake-word model listens for the shape of the phrase instead, in ~1 ms per 80 ms
of audio, and never needs the cloud.

Three small ONNX models (openWakeWord's pipeline): audio → mel spectrogram → a 96-number embedding per
80 ms → the wake model scores the last 16 embeddings (~1.3 s). The `openwakeword` package itself won't
install here (it pins a Linux-only tflite wheel), so this runs the same models directly.

The pretrained phrase is "Hey Jarvis" (model licence CC BY-NC-SA 4.0: fine for a personal assistant). A
custom "Hey Sommus" model trained with openWakeWord's notebook drops in through `[voice] wake_model`.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
FEATURE_MODELS = ("melspectrogram.onnx", "embedding_model.onnx")
DEFAULT_MODEL = "hey_jarvis_v0.1.onnx"
SAMPLE_RATE = 16_000
FRAME = 1280  # 80 ms: one embedding per frame
MEL_WINDOW = 76  # mel frames per embedding
EMBEDDINGS = 16  # embeddings per wake score
COOLDOWN_SECONDS = 2.0  # one "Hey Jarvis" is one wake, not five


def fetch(name: str, folder: Path) -> Path:
    """A model file, downloaded once from openWakeWord's release into `folder`."""
    path = folder / name
    if not path.exists():
        import httpx2  # not urllib: this Python has no CA certificates

        folder.mkdir(parents=True, exist_ok=True)
        response = httpx2.get(RELEASE + name, timeout=60, follow_redirects=True)
        response.raise_for_status()
        part = path.with_suffix(".part")
        part.write_bytes(response.content)
        part.rename(path)
    return path


def _session(path: Path):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1  # leave the cores to Whisper and the echo canceller
    return ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])


class WakeWord:
    """Feed it 16 kHz mono float audio in any size of chunk; `feed` says when the phrase was just heard."""

    def __init__(self, folder: Path, model: str = DEFAULT_MODEL, threshold: float = 0.5):
        self.folder = folder
        self.model = model
        self.threshold = threshold
        self.last_score = 0.0
        self._sessions: tuple | None = None
        self._pending = np.zeros(0, dtype=np.float32)
        self._cooldown = 0

    def warm_up(self) -> None:
        """Download (once, ~3.5 MB) and load the models, then fill the history so the first score is sane."""
        model_path = Path(self.model)
        if not model_path.is_absolute():
            model_path = fetch(self.model, self.folder)
        mel, embed = (_session(fetch(name, self.folder)) for name in FEATURE_MODELS)
        self._sessions = (mel, embed, _session(model_path))
        self.reset()

    def reset(self) -> None:
        """Forget what was heard: after a wake, or when the microphone changes hands."""
        if self._sessions is None:
            self.warm_up()
            return
        self._raw = np.zeros(FRAME + 480, dtype=np.float32)
        self._mel = np.ones((MEL_WINDOW, 32), dtype=np.float32)
        self._embeddings = np.zeros((0, 96), dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        noise = np.random.default_rng(0).integers(-1000, 1000, SAMPLE_RATE * 4).astype(np.float32) / 32768
        for start in range(0, len(noise), FRAME):
            self._frame(noise[start : start + FRAME])
        self._cooldown = 0
        self.last_score = 0.0

    def _frame(self, frame: np.ndarray) -> float:
        mel, embed, wake = self._sessions
        # The mel model wants int16-scaled floats, with 3 hops of the previous frame for context.
        self._raw = np.concatenate([self._raw[-480:], frame])
        spec = mel.run(None, {"input": (self._raw * 32767)[None, :].astype(np.float32)})[0]
        spec = np.squeeze(spec) / 10 + 2  # openWakeWord's scaling of the mel output
        self._mel = np.concatenate([self._mel, spec.reshape(-1, 32)])[-MEL_WINDOW * 2 :]
        window = self._mel[-MEL_WINDOW:][None, :, :, None].astype(np.float32)
        vector = embed.run(None, {"input_1": window})[0].reshape(1, 96)
        self._embeddings = np.concatenate([self._embeddings, vector])[-EMBEDDINGS:]
        if len(self._embeddings) < EMBEDDINGS:
            return 0.0
        return float(wake.run(None, {"x.1": self._embeddings[None, :, :]})[0].reshape(-1)[0])

    def feed(self, audio: np.ndarray) -> bool:
        """True the moment the wake phrase ends. Scores every complete 80 ms frame in `audio`."""
        if self._sessions is None:
            self.warm_up()
        self._pending = np.concatenate([self._pending, audio.astype(np.float32)])
        heard = False
        while len(self._pending) >= FRAME:
            frame, self._pending = self._pending[:FRAME], self._pending[FRAME:]
            score = self._frame(frame)
            self.last_score = max(self.last_score * 0.9, score)
            if self._cooldown > 0:
                self._cooldown -= 1
            elif score >= self.threshold:
                heard = True
                self._cooldown = round(COOLDOWN_SECONDS * SAMPLE_RATE / FRAME)
        return heard
