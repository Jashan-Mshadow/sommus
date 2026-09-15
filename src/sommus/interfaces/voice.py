"""Voice interface: speak to Sommus and hear it answer.

Another interface over the same brain — it turns speech into text going in and text
into speech coming out. Everything audio happens on this machine: recording, speech-to-
text (Whisper on Apple silicon) and the voice (macOS `say`). Only the text reaches the
model, so this runs unchanged once the brain lives somewhere else.

V1 + V2: press Enter, talk, and it stops listening when you go quiet. The wake word
("Hey Sommus") replaces the Enter key in V3.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16_000
FRAME_SECONDS = 0.03
CHIME_START = "/System/Library/Sounds/Tink.aiff"
CHIME_STOP = "/System/Library/Sounds/Pop.aiff"


# ---------------------------------------------------------------- speaking


SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


def clean_for_speech(text: str) -> str:
    """What reads well on screen often sounds wrong aloud."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)  # code blocks
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [label](url) -> label, before bare URLs
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[*`#>|]", "", text)  # markdown symbols (underscores stay: file_names read fine)
    text = text.replace("→", " to ").replace("—", ", ").replace("·", ",")
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"\s+([,.!?])", r"\1", text).strip(" ,")


class Speaker:
    """Speaks a streaming reply sentence by sentence, so the first words start early."""

    def __init__(self, voice: str, rate: int = 190):
        self.voice = voice
        self.rate = rate
        self.buffer = ""
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.current: asyncio.subprocess.Process | None = None
        self.worker = asyncio.create_task(self._run())
        self.first_word_at: float | None = None

    async def _run(self) -> None:
        while True:
            sentence = await self.queue.get()
            if sentence is None:
                self.queue.task_done()
                return
            if self.first_word_at is None:
                self.first_word_at = time.monotonic()
            self.current = await asyncio.create_subprocess_exec(
                "say",
                "-v",
                self.voice,
                "-r",
                str(self.rate),
                sentence,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await self.current.wait()
            self.current = None
            self.queue.task_done()

    def feed(self, delta: str) -> None:
        self.buffer += delta
        parts = SENTENCE_END.split(self.buffer)
        for sentence in parts[:-1]:
            self._say(sentence)
        self.buffer = parts[-1]

    def flush(self) -> None:
        self._say(self.buffer)
        self.buffer = ""

    def _say(self, sentence: str) -> None:
        spoken = clean_for_speech(sentence)
        if spoken:
            self.queue.put_nowait(spoken)

    async def finished(self) -> None:
        await self.queue.join()

    def interrupt(self) -> None:
        """Stop talking now: drop what's queued and cut off the current sentence."""
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        if self.current and self.current.returncode is None:
            self.current.kill()

    async def close(self) -> None:
        self.interrupt()
        self.queue.put_nowait(None)
        await self.worker


# ---------------------------------------------------------------- listening


@dataclass
class SilenceDetector:
    """Decides, frame by frame, when someone has started and then finished talking.

    The noise floor is measured first, so a quiet dorm room and a loud one both work.
    """

    silence_seconds: float = 0.9
    wait_seconds: float = 8.0
    max_seconds: float = 20.0
    calibration_frames: int = 10

    def __post_init__(self) -> None:
        self.levels: list[float] = []
        self.threshold = 0.0
        self.heard_speech = False
        self.quiet_frames = 0
        self.frames = 0

    def update(self, rms: float) -> str:
        """Returns 'calibrating', 'waiting', 'speaking', 'done', or 'timeout'."""
        self.frames += 1
        elapsed = self.frames * FRAME_SECONDS
        if self.frames <= self.calibration_frames:
            self.levels.append(rms)
            if self.frames == self.calibration_frames:
                self.threshold = max(float(np.median(self.levels)) * 3.0, 0.008)
            return "calibrating"
        if rms > self.threshold:
            self.heard_speech = True
            self.quiet_frames = 0
        elif self.heard_speech:
            self.quiet_frames += 1
        if self.heard_speech and self.quiet_frames * FRAME_SECONDS >= self.silence_seconds:
            return "done"
        if self.heard_speech and elapsed >= self.max_seconds:
            return "done"
        if not self.heard_speech and elapsed >= self.wait_seconds:
            return "timeout"
        return "speaking" if self.heard_speech else "waiting"


def record_until_silence(detector: SilenceDetector, device: str | int | None = None) -> np.ndarray | None:
    """Record from the microphone until the speaker goes quiet. None if nobody spoke."""
    import sounddevice as sd

    frames: list[np.ndarray] = []
    blocksize = int(SAMPLE_RATE * FRAME_SECONDS)
    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=blocksize, device=device
    ) as stream:
        while True:
            block, _ = stream.read(blocksize)
            mono = block[:, 0].copy()
            frames.append(mono)
            state = detector.update(float(np.sqrt(np.mean(mono**2))))
            if state == "done":
                return np.concatenate(frames)
            if state == "timeout":
                return None


# ---------------------------------------------------------------- understanding

# Whisper invents these when handed near-silence or noise.
HALLUCINATIONS = {"", "you", "thank you", "thanks for watching", "bye", "thank you for watching"}


class Transcriber:
    def __init__(self, model: str):
        self.model = model

    def warm_up(self) -> None:
        """The first call loads the model (~1s); do it before the user is waiting."""
        self.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))

    def transcribe(self, audio: np.ndarray) -> str:
        import mlx_whisper

        result = mlx_whisper.transcribe(audio, path_or_hf_repo=self.model, language="en", fp16=True)
        text = result["text"].strip()
        return "" if text.lower().strip(" .!?") in HALLUCINATIONS else text


def chime(path: str) -> None:
    subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
