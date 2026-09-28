"""Has he finished talking? Smart Turn v3 (Pipecat, BSD-2) answers from the sound itself.

A fixed pause can't tell "remind me to... [thinking] ...call Didi" from "what's the time." [done]: too
short and it cuts him off mid-thought, too long and every reply waits. Smart Turn listens to the
intonation and rhythm of the last 8 s — a falling "done" tone vs. a trailing "and..." — and says whether
the turn is complete. It runs on the CPU in tens of milliseconds, entirely on the Mac.

Used with the speech detector: at a short pause Sommus asks Smart Turn; "complete" ends the utterance
straight away, "not yet" keeps listening until a longer pause.
"""

from __future__ import annotations

import numpy as np

REPO = "pipecat-ai/smart-turn-v3"
MODEL_FILE = "smart-turn-v3.2-cpu.onnx"  # int8, ~9 MB
SAMPLE_RATE = 16_000
WINDOW_SECONDS = 8


def last_window(audio: np.ndarray) -> np.ndarray:
    """The last 8 s, or zeros in front to make 8 s: the model expects the speech at the end."""
    size = WINDOW_SECONDS * SAMPLE_RATE
    if len(audio) >= size:
        return audio[-size:]
    return np.pad(audio, (size - len(audio), 0))


class SmartTurn:
    def __init__(self, model_path: str | None = None):
        self.model_path = model_path
        self._session = None
        self._features = None

    def warm_up(self) -> None:
        """Load the model (downloaded once, ~9 MB) and the feature extractor before anyone is waiting."""
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor

        if self.model_path is None:
            from huggingface_hub import hf_hub_download

            self.model_path = hf_hub_download(REPO, MODEL_FILE)
        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1  # leave the cores to Whisper and Kokoro
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(self.model_path, sess_options=options)
        self._features = WhisperFeatureExtractor(chunk_length=WINDOW_SECONDS)
        self.complete(np.zeros(SAMPLE_RATE, dtype=np.float32))

    def complete(self, audio: np.ndarray) -> float:
        """Probability (0–1) that the speaker has finished their turn."""
        if self._session is None:
            self.warm_up()
        features = self._features(
            last_window(audio.astype(np.float32)),
            sampling_rate=SAMPLE_RATE,
            return_tensors="np",
            padding="max_length",
            max_length=WINDOW_SECONDS * SAMPLE_RATE,
            truncation=True,
            do_normalize=True,
        ).input_features.astype(np.float32)
        return float(self._session.run(None, {"input_features": features})[0].reshape(-1)[0])
