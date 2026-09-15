"""Voice interface: speak to Sommus and hear it answer.

Another interface over the same brain — it turns speech into text going in and text
into speech coming out. Everything audio happens on this machine: recording, speech-to-
text (Whisper on Apple silicon) and the voice (Kokoro on Apple silicon, or macOS `say`).
Only the text reaches the model, so this runs unchanged once the brain lives somewhere else.

Press Return, talk, and it stops listening when you go quiet. The wake word
("Hey Sommus") replaces the Return key in V4.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

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
    text = re.sub(r"\b(\d{1,2}):00\b", r"\1", text)  # 3:00 PM -> 3 PM
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", r"\1 \2", text)  # 2:45 -> 2 45, or Kokoro skips the colon's word
    text = re.sub(r"\s*°\s*[CF]?(?![A-Za-z])", " degrees", text)
    text = re.sub(r"(?<=\d)\s*[–-]\s*(?=\d)", " to ", text)  # 8:30–10:20 -> 8 30 to 10 20
    text = re.sub(r"(?<=\w)/(?=[A-Za-z]\w*\s?\d)", " or ", text)  # E7 1427/E2 1792 -> E7 1427 or E2 1792
    text = text.replace("→", " to ").replace("—", ", ").replace("–", ", ").replace("·", ",")
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"\s+([,.!?])", r"\1", text).strip(" ,")


# The name is said SO-mis. Kokoro takes phonemes inline (misaki's "O" is American oʊ, "Q" British əʊ);
# `say` gets a spelling that comes out right; Whisper's usual spellings of it are mapped back.
NAME = re.compile(r"\bsommus\b", re.I)
NAME_PHONEMES = {"a": "sˈOmɪs", "b": "sˈQmɪs"}
NAME_SAY = "Sowmiss"
NAME_HEARD = re.compile(r"\b(?:somm?iss?|sowmiss?|somm?us|summus|samus)\b|^(?:hey,?\s+)?so,?\s+miss\b", re.I)


def _run_until_done(command: list[str], stop: threading.Event) -> None:
    """Run a player process to the end, or kill it the moment `stop` is set."""
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while process.poll() is None:
        if stop.wait(0.02):
            process.kill()
            process.wait()
            return


def quiet_hugging_face(model: str) -> None:
    """Once a model is on disk, skip Hugging Face's online check and its progress bars,
    which otherwise print a screen of "Downloading 0.00B" on every start."""
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    cached = Path.home() / ".cache/huggingface/hub" / f"models--{model.replace('/', '--')}"
    if cached.is_dir():
        os.environ.setdefault("HF_HUB_OFFLINE", "1")


class SayVoice:
    """macOS `say`: always there and instant, but robotic. The fallback voice."""

    def __init__(self, voice: str = "Samantha", rate: int = 190):
        self.voice = voice
        self.rate = rate

    @property
    def label(self) -> str:
        return f"{self.voice} (macOS say)"

    def warm_up(self) -> None:
        pass

    def synthesize(self, sentence: str) -> str:
        return sentence  # `say` makes the audio as it plays

    def play(self, sentence: str, stop: threading.Event) -> None:
        _run_until_done(["say", "-v", self.voice, "-r", str(self.rate), NAME.sub(NAME_SAY, sentence)], stop)

    def rest(self, stop: threading.Event) -> None:
        pass  # each `say` finishes on its own


ENGLISH_VOICE = r"[ab][fm]_[a-z]+"


def english_voice(name: str) -> str:
    """Only English voices work: misaki's English phonemizer is the one installed.
    The first letter is the accent (a = American, b = British), the second f/m."""
    if not re.fullmatch(ENGLISH_VOICE, name):
        raise ValueError(f"'{name}' isn't an English Kokoro voice — use one like af_heart, am_michael or bf_emma")
    return name


class KokoroVoice:
    """Kokoro-82M through mlx-audio: a natural voice, free, and offline once downloaded (~330 MB).

    Makes a sentence of audio in about a sixth of the time it takes to say it on an M1.
    One output stream stays open for a whole reply, so sentences run together without the
    gap a player process leaves (`afplay` hangs on ~1.3 s after the audio ends).
    """

    SAMPLE_RATE = 24_000

    def __init__(self, voice: str = "af_heart", speed: float = 1.0, model: str = "mlx-community/Kokoro-82M-bf16"):
        self.voice = english_voice(voice)
        self.speed = speed
        self.model_id = model
        self.model = None
        self._stream = None

    @property
    def label(self) -> str:
        return f"{self.voice} (Kokoro)"

    def warm_up(self) -> None:
        """Load the model (~1 s) and build the phoneme pipeline (~4 s) before anyone is waiting."""
        import warnings

        quiet_hugging_face(self.model_id)
        warnings.filterwarnings("ignore", category=FutureWarning)  # torch.jit notice from a dependency
        # espeak reports "words count mismatch" for every course code it spells out; the audio is fine.
        logging.getLogger("phonemizer").setLevel(logging.ERROR)
        from mlx_audio.tts.utils import load_model

        self.model = load_model(self.model_id)
        self.model.repo_id = self.model_id  # voices come from the same download, not a second repo fetched per voice
        self.synthesize("Ready.")

    def english_voices(self) -> list[str]:
        """Every English voice in the downloaded model, e.g. af_heart, bm_george."""
        from huggingface_hub import snapshot_download

        folder = (
            Path(snapshot_download(self.model_id, allow_patterns=["voices/*.safetensors"], local_files_only=True))
            / "voices"
        )
        return sorted(v.stem for v in folder.glob("*.safetensors") if re.fullmatch(ENGLISH_VOICE, v.stem))

    def synthesize(self, sentence: str) -> np.ndarray:
        if self.model is None:
            self.warm_up()
        sentence = NAME.sub(f"[Sommus](/{NAME_PHONEMES[self.voice[0]]}/)", sentence)
        chunks = self.model.generate(text=sentence, voice=self.voice, speed=self.speed, lang_code=self.voice[0])
        parts = [np.asarray(chunk.audio, dtype=np.float32) for chunk in chunks]
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    def play(self, audio: np.ndarray, stop: threading.Event) -> None:
        import sounddevice as sd

        if self._stream is None:
            _refresh_audio_devices()
            self._stream = sd.OutputStream(samplerate=self.SAMPLE_RATE, channels=1, dtype="float32")
            self._stream.start()
        block = self.SAMPLE_RATE // 20  # check for an interruption every 50 ms
        for start in range(0, len(audio), block):
            if stop.is_set():
                self.rest(stop)
                return
            self._stream.write(audio[start : start + block])

    def rest(self, stop: threading.Event) -> None:
        """The reply is over: let the last words play out (or drop them if interrupted) and free the output."""
        stream, self._stream = self._stream, None
        if stream is None:
            return
        if stop.is_set():
            stream.abort()
        else:
            stream.stop()  # waits for buffered audio; close() alone would cut off the last word
        stream.close()


def _refresh_audio_devices() -> None:
    """PortAudio reads the device list once, at startup. Re-read it before each reply so speech goes
    to the current output — AirPods connected after Sommus started, not the speakers."""
    import sounddevice as sd

    sd._terminate()
    sd._initialize()


def make_voice(settings: dict) -> SayVoice | KokoroVoice:
    """The voice named in [voice] config. `engine = "say"` keeps the built-in macOS voice."""
    say = SayVoice(settings.get("say_voice", "Samantha"), int(settings.get("say_rate", 190)))
    if settings.get("engine", "kokoro") == "say":
        return say
    return KokoroVoice(
        settings.get("kokoro_voice", "af_heart"),
        float(settings.get("speed", 1.0)),
        settings.get("kokoro_model", "mlx-community/Kokoro-82M-bf16"),
    )


class Speaker:
    """Speaks a streaming reply sentence by sentence, so the first words start early.

    Two stages run side by side: sentences become audio as soon as they arrive, and audio
    plays in order — so the next sentence is usually ready when the current one ends.
    """

    def __init__(self, engine: SayVoice | KokoroVoice, on_error: Callable[[Exception], None] | None = None):
        self.engine = engine
        self.on_error = on_error
        self.buffer = ""
        self.first_word_at: float | None = None
        self._epoch = 0  # bumped by interrupt(); anything queued from an older epoch is dropped
        self._stop = threading.Event()
        self._text: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
        self._audio: asyncio.Queue[tuple[int, object] | None] = asyncio.Queue()
        self._unfinished = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._workers = [asyncio.create_task(self._synthesize()), asyncio.create_task(self._play())]

    async def _synthesize(self) -> None:
        while (item := await self._text.get()) is not None:
            epoch, sentence = item
            clip = None
            if epoch == self._epoch:
                try:
                    clip = await asyncio.to_thread(self.engine.synthesize, sentence)
                except Exception as e:  # a broken voice must not take the conversation down with it
                    if self.on_error:
                        self.on_error(e)
            self._audio.put_nowait((epoch, clip))
        self._audio.put_nowait(None)

    async def _play(self) -> None:
        while (item := await self._audio.get()) is not None:
            epoch, clip = item
            if epoch == self._epoch and clip is not None:
                if self._stop.is_set():  # a reply was cut off: throw away its buffered audio first
                    await asyncio.to_thread(self.engine.rest, self._stop)
                    self._stop.clear()
                if self.first_word_at is None:
                    self.first_word_at = time.monotonic()
                await asyncio.to_thread(self.engine.play, clip, self._stop)
            if self._unfinished == 1:  # nothing else to say for now
                await asyncio.to_thread(self.engine.rest, self._stop)
            self._unfinished -= 1
            if self._unfinished == 0:
                self._idle.set()

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
            self._unfinished += 1
            self._idle.clear()
            self._text.put_nowait((self._epoch, spoken))

    async def finished(self) -> None:
        await self._idle.wait()

    def interrupt(self) -> None:
        """Stop talking now: drop what's queued and cut off the current sentence."""
        self.buffer = ""
        self._epoch += 1
        self._stop.set()

    async def close(self) -> None:
        self.interrupt()
        self._text.put_nowait(None)
        await asyncio.gather(*self._workers)


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
    stream, rate = open_microphone(device)
    frames: list[np.ndarray] = []
    blocksize = int(rate * FRAME_SECONDS)
    with stream:
        while True:
            block, _ = stream.read(blocksize)
            mono = block[:, 0].copy()
            frames.append(mono)
            state = detector.update(float(np.sqrt(np.mean(mono**2))))
            if state == "done":
                return to_16k(np.concatenate(frames), rate)
            if state == "timeout":
                return None


def open_microphone(device: str | int | None = None):
    """An open input stream and its sample rate, getting past the ways Core Audio refuses one.

    PortAudio's device list goes stale when AirPods or an iPhone mic come and go ("!obj",
    PaErrorCode -9986), and Bluetooth mics can reject 16 kHz (-10851). So: re-read the devices,
    try 16 kHz, then the mic's own rate, then the built-in microphone.
    """
    import sounddevice as sd

    _refresh_audio_devices()
    attempts: list[tuple[str | int | None, float]] = [(device, SAMPLE_RATE)]
    try:
        attempts.append((device, float(sd.query_devices(device, kind="input")["default_samplerate"])))
    except Exception:
        pass
    built_in = next((d["name"] for d in sd.query_devices() if d["max_input_channels"] and "MacBook" in d["name"]), None)
    if built_in and built_in != device:
        attempts.append((built_in, SAMPLE_RATE))
    error: Exception | None = None
    for name, rate in attempts:
        try:
            stream = sd.InputStream(
                samplerate=rate, channels=1, dtype="float32", blocksize=int(rate * FRAME_SECONDS), device=name
            )
            stream.start()
            return stream, int(rate)
        except Exception as e:
            error = e
    raise error or RuntimeError("no microphone found")


def to_16k(audio: np.ndarray, rate: int) -> np.ndarray:
    """Whisper wants 16 kHz."""
    if rate == SAMPLE_RATE:
        return audio
    from scipy.signal import resample_poly

    divisor = np.gcd(SAMPLE_RATE, rate)
    return resample_poly(audio, SAMPLE_RATE // divisor, rate // divisor).astype(np.float32)


# ---------------------------------------------------------------- understanding

# Whisper invents these when handed near-silence or noise.
HALLUCINATIONS = {"", "you", "thank you", "thanks for watching", "bye", "thank you for watching"}


class Transcriber:
    def __init__(self, model: str):
        self.model = model

    def warm_up(self) -> None:
        """The first call loads the model (~1s); do it before the user is waiting."""
        quiet_hugging_face(self.model)
        self.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))

    def transcribe(self, audio: np.ndarray) -> str:
        import mlx_whisper

        result = mlx_whisper.transcribe(audio, path_or_hf_repo=self.model, language="en", fp16=True)
        text = result["text"].strip()
        return "" if text.lower().strip(" .!?") in HALLUCINATIONS else NAME_HEARD.sub("Sommus", text)


def chime(path: str) -> None:
    subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
