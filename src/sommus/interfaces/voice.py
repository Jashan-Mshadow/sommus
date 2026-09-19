"""Voice interface: speak to Sommus and hear it answer.

Another interface over the same brain — it turns speech into text going in and text
into speech coming out. Everything audio happens on this machine: recording, speech-to-
text (Whisper on Apple silicon) and the voice (Kokoro on Apple silicon, or macOS `say`).
Only the text reaches the model, so this runs unchanged once the brain lives somewhere else.

Always listening (V4): a speech detector cuts the microphone into utterances, Whisper reads
each one on the Mac, and only one that starts or ends with a wake phrase ("Hey Sommus") wakes it.
Awake, everything said is a command until the conversation goes quiet; then it sleeps again.
Nothing is sent anywhere before the wake phrase, and the mic is deaf while Sommus talks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE = 16_000
CHUNK = 512  # Silero VAD's frame at 16 kHz: 32 ms
CHUNK_SECONDS = CHUNK / SAMPLE_RATE
CHIME_WAKE = "/System/Library/Sounds/Tink.aiff"
CHIME_SLEEP = "/System/Library/Sounds/Pop.aiff"


# ---------------------------------------------------------------- speaking


SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


CLASS_TYPES = {"LEC": "lecture", "TUT": "tutorial", "LAB": "lab", "SEM": "seminar", "TST": "test"}


def say_courses(text: str, say_as: dict[str, str]) -> str:
    """'ECE105 LEC 001' -> 'physics lecture'. Codes are for screens; people say the subject."""
    for code, name in say_as.items():
        letters, number = re.match(r"([A-Za-z]+)\s*(\d+)", code).groups()
        text = re.sub(rf"\b{letters}\s?{number}\b", name, text, flags=re.I)
    kinds = "|".join(CLASS_TYPES)
    text = re.sub(
        rf"\b({kinds})\b(?:\s*\d{{3}}\b)?",
        lambda m: CLASS_TYPES[m.group(1).upper()],
        text,
        flags=re.I,
    )
    return re.sub(r"\b(lecture|tutorial|lab|seminar)\s+(\d{3})\b", r"\1", text, flags=re.I)  # "Lab 001"


def clean_for_speech(text: str, say_as: dict[str, str] | None = None) -> str:
    """What reads well on screen often sounds wrong aloud."""
    if say_as:
        text = say_courses(text, say_as)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)  # code blocks
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [label](url) -> label, before bare URLs
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[*`#>|]", "", text)  # markdown symbols (underscores stay: file_names read fine)
    text = re.sub(r"\b(\d{1,2}):00\b", r"\1", text)  # 3:00 PM -> 3 PM
    text = re.sub(r"\b(\d{1,2}):0(\d)\b", r"\1 oh \2", text)  # 10:05 -> ten oh five, not "ten five"
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
        self.output = None  # a DuplexAudio in barge-in mode

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

        if self.output is not None:  # barge-in mode: through the echo-cancelling engine
            self.output.play(audio, stop)
            return
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
        if self.output is not None:
            return  # the engine keeps running for the mic
        stream, self._stream = self._stream, None
        if stream is None:
            return
        if stop.is_set():
            stream.abort()
        else:
            stream.stop()  # waits for buffered audio; close() alone would cut off the last word
        stream.close()


# Held while a PortAudio stream is open. Re-reading the device list restarts PortAudio, and a
# microphone stream open across that restart silently stops delivering audio (found in use: the
# first command worked, then Sommus heard nothing and couldn't be woken).
AUDIO_DEVICES = threading.RLock()


def _refresh_audio_devices() -> None:
    """PortAudio reads the device list once, at startup. Re-read it before each reply so speech goes
    to the current output — AirPods connected after Sommus started, not the speakers. Waits for
    the listener to close the microphone first."""
    import sounddevice as sd

    with AUDIO_DEVICES:
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

    def __init__(
        self,
        engine: SayVoice | KokoroVoice,
        on_error: Callable[[Exception], None] | None = None,
        say_as: dict[str, str] | None = None,
    ):
        self.engine = engine
        self.on_error = on_error
        self.say_as = say_as or {}
        self.buffer = ""
        self.first_word_at: float | None = None
        self._epoch = 0  # bumped by interrupt(); anything queued from an older epoch is dropped
        self._stop = threading.Event()
        self._text: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
        self._audio: asyncio.Queue[tuple[int, object] | None] = asyncio.Queue()
        self._unfinished = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self.quiet_since = 0.0  # when the last words finished playing — the room still echoes briefly
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
                self.quiet_since = time.monotonic()
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
        spoken = clean_for_speech(sentence, self.say_as)
        if spoken:
            self._unfinished += 1
            self._idle.clear()
            self._text.put_nowait((self._epoch, spoken))

    @property
    def speaking(self) -> bool:
        return not self._idle.is_set()

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


class SpeechDetector:
    """Cuts a continuous microphone stream into utterances.

    Silero VAD scores each 32 ms chunk for speech (~0.25 ms of CPU each, under 1% of a core), so
    fans, typing and music don't wake Whisper — plain loudness couldn't tell them from a voice.
    A short pre-roll keeps the first syllable ("hey") that the detector needs a moment to catch.
    """

    def __init__(
        self,
        probability: Callable[[np.ndarray], float] | None = None,
        silence_seconds: float = 0.9,
        min_speech_seconds: float = 0.25,
        max_seconds: float = 20.0,
        pre_roll_seconds: float = 0.5,
        threshold: float = 0.5,
    ):
        self._probability = probability
        self._model = None
        self.silence_chunks = round(silence_seconds / CHUNK_SECONDS)
        self.min_speech_chunks = round(min_speech_seconds / CHUNK_SECONDS)
        self.max_chunks = round(max_seconds / CHUNK_SECONDS)
        self.pre_roll: deque[np.ndarray] = deque(maxlen=round(pre_roll_seconds / CHUNK_SECONDS))
        self.threshold = threshold
        self.reset()

    def probability(self, chunk: np.ndarray) -> float:
        if self._probability:
            return self._probability(chunk)
        if self._model is None:
            import warnings

            import torch
            from silero_vad import load_silero_vad

            warnings.filterwarnings("ignore", category=FutureWarning)  # torch.jit.load notice
            torch.set_num_threads(1)
            self._model = load_silero_vad()
        import torch

        return float(self._model(torch.from_numpy(chunk), SAMPLE_RATE).item())

    def reset(self) -> None:
        self.buffer: list[np.ndarray] = []
        self.speech = self.silence = 0
        self.pre_roll.clear()
        if self._model is not None:
            self._model.reset_states()

    def feed(self, chunk: np.ndarray) -> np.ndarray | None:
        """One 512-sample chunk in; a finished utterance out, or None."""
        score = self.probability(chunk)
        if not self.buffer:
            self.pre_roll.append(chunk)
            if score >= self.threshold:
                self.buffer, self.speech, self.silence = list(self.pre_roll), 1, 0
            return None
        self.buffer.append(chunk)
        if score >= self.threshold - 0.15:  # a little easier to stay in speech than to start it
            self.speech, self.silence = self.speech + 1, 0
        else:
            self.silence += 1
        if self.silence >= self.silence_chunks or len(self.buffer) >= self.max_chunks:
            utterance, long_enough = np.concatenate(self.buffer), self.speech >= self.min_speech_chunks
            self.reset()
            return utterance if long_enough else None
        return None


class Listener:
    """The always-on microphone, in a background thread. Finished utterances go to `deliver`.

    Deaf while `deaf()` says so — Sommus talking, a chime playing, a turn being worked on — so it
    never hears itself. If the microphone disappears (AirPods leave), it keeps trying to reopen.
    In barge-in mode the source is the echo-cancelled mic, it stays open while Sommus talks, and
    `on_speech` fires once someone has been talking for a moment, to cut Sommus off.
    """

    BARGE_IN_SECONDS = 0.3  # a cough or a clink shouldn't stop Sommus mid-sentence

    def __init__(
        self,
        detector: SpeechDetector,
        deliver: Callable[[np.ndarray], None],
        deaf: Callable[[], bool],
        device: str | int | None = None,
        on_error: Callable[[Exception], None] | None = None,
        source: Callable[[], tuple[Any, int]] | None = None,
        on_speech: Callable[[], None] | None = None,
    ):
        self.detector = detector
        self.deliver = deliver
        self.deaf = deaf
        self.device = device
        self.on_error = on_error
        self.source = source or (lambda: open_microphone(self.device))
        self.on_speech = on_speech
        self._speech_reported = False
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sommus-listener", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        reported = False
        while not self._stopping.is_set():
            if self.deaf():  # the mic stays closed while Sommus talks or works, freeing PortAudio
                self._stopping.wait(0.05)
                continue
            try:
                with AUDIO_DEVICES:
                    stream, rate = self.source()
                    with stream:
                        self._listen(stream, rate)
                reported = False
            except Exception as e:  # device gone, permission missing: say so once, keep trying
                if not reported and self.on_error:
                    self.on_error(e)
                reported = True
                self._stopping.wait(2)

    def _listen(self, stream, rate: int) -> None:
        """Feed the detector until Sommus needs the audio system (deaf) or the listener stops."""
        block = round(rate * CHUNK_SECONDS)
        while not self._stopping.is_set():
            audio, _ = stream.read(block)
            if self.deaf():
                self.detector.reset()
                return
            mono = audio[:, 0]
            if rate != SAMPLE_RATE:
                mono = np.interp(np.linspace(0, len(mono) - 1, CHUNK), np.arange(len(mono)), mono)
            utterance = self.detector.feed(np.ascontiguousarray(mono, dtype=np.float32))
            talking = self.detector.speech * CHUNK_SECONDS >= self.BARGE_IN_SECONDS
            if self.on_speech and talking and not self._speech_reported:
                self._speech_reported = True
                self.on_speech()
            if utterance is not None or not self.detector.buffer:
                self._speech_reported = False
            if utterance is not None:
                self.deliver(utterance)


# How Whisper writes the words of a wake phrase. SO-mis often comes out as "so miss" or "so missy",
# which is harmless to accept here: only the start or end of an utterance can wake Sommus.
WAKE_WORDS = {
    "sommus": r"(?:sommus|so[\s,.-]*miss(?:y|es)?|somm?iss?|sowmiss?|summus|samus)",
    "whats": r"what'?s",
}


def wake_pattern(phrases: list[str]) -> re.Pattern[str]:
    """One regex for every wake phrase, tolerant of Whisper's punctuation: "What's up, Sommus?"."""
    alternatives = []
    for phrase in sorted(phrases, key=len, reverse=True):
        words = re.findall(r"[a-z]+", phrase.lower().replace("'", ""))
        alternatives.append(r"[\s,.!?'-]*".join(WAKE_WORDS.get(w, re.escape(w)) for w in words))
    return re.compile(rf"(?:{'|'.join(alternatives)})", re.I)


def heard_wake(text: str, pattern: re.Pattern[str]) -> str | None:
    """The request after (or before) a wake phrase: '' for the name alone, None if it wasn't said.

    Only at the start or the end: "Hey Sommus, mute" and "what time is it, Sommus?" wake it,
    "I'm working on Sommus tonight" doesn't.
    """
    stripped = text.strip()
    start = re.match(rf"^[\s,.!?]*(?:{pattern.pattern})\b[\s,.!?]*", stripped, re.I)
    if start:
        return stripped[start.end() :].strip()
    end = re.search(rf"[\s,.!?]*\b(?:{pattern.pattern})[\s,.!?]*$", stripped, re.I)
    if end:
        return stripped[: end.start()].strip()
    # Mid-utterance, but set off like a name ("…see? So, wait, Sommus, what's my battery?"): the
    # detector merged talk to someone else with the request, so keep what follows the name.
    middle = list(re.finditer(rf"[,.!?]\s*(?:{pattern.pattern})\s*[,.!?]\s*", stripped, re.I))
    if middle:
        return stripped[middle[-1].end() :].strip()
    return None


# A sentence ending on one of these stopped mid-thought ("remind me to", "text mom and"): a pause, not
# the end. Words that also end complete requests ("turn it on", "what's the weather like") aren't here.
UNFINISHED = {
    "and", "but", "or", "to", "the", "a", "an", "my", "your", "of", "with", "for", "because", "if", "um", "uh",
    "than", "into",
}  # fmt: skip


def sounds_unfinished(text: str) -> bool:
    """Whisper puts a full stop on almost everything, so judge by the last word instead."""
    stripped = text.strip()
    if not stripped or stripped.endswith("?"):
        return False
    if stripped.endswith(("...", "…", ",", "-", "—")):
        return True
    words = re.findall(r"[a-z']+", stripped.lower())
    return bool(words) and words[-1] in UNFINISHED


# Said to end the conversation rather than as a request.
DISMISS = re.compile(
    r"^\W*(?:that'?s (?:all|it)|thanks?(?: you)?|never ?mind|go to sleep|stop listening|good ?bye|bye|"
    r"nothing|no thanks|i'?m good|all good|we'?re done|done)\W*$",
    re.I,
)


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
                samplerate=rate, channels=1, dtype="float32", blocksize=round(rate * CHUNK_SECONDS), device=name
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


def normalize(audio: np.ndarray, target_peak: float = 0.7) -> np.ndarray:
    """Bring a quiet recording up before Whisper reads it.

    Speech from across the room arrives at a fraction of full scale, and Whisper hears it as
    mumbling or as nothing. Scaling the loudest sample to a fixed level costs nothing and doesn't
    change what was said; the cap keeps a hiss-only clip from being amplified into noise.
    """
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak < 0.005 or peak >= target_peak:  # silence, or already loud enough
        return audio
    return (audio * min(target_peak / peak, 20.0)).astype(np.float32)


class Transcriber:
    def __init__(self, model: str):
        self.model = model

    def warm_up(self) -> None:
        """The first call loads the model (~1s); do it before the user is waiting."""
        quiet_hugging_face(self.model)
        self.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))

    def transcribe(self, audio: np.ndarray, expecting: str | None = None) -> str:
        """`expecting="digits"` when a PIN was asked for: Whisper writes what it expects, and a
        half-caught number comes out as words ("day four") unless it is told numbers are coming."""
        import mlx_whisper

        hint = {"initial_prompt": "My PIN is 1234."} if expecting == "digits" else {}
        result = mlx_whisper.transcribe(normalize(audio), path_or_hf_repo=self.model, language="en", fp16=True, **hint)
        text = result["text"].strip()
        return "" if text.lower().strip(" .!?") in HALLUCINATIONS else NAME_HEARD.sub("Sommus", text)


def chime(path: str) -> None:
    subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
