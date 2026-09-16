"""Mic and speaker on one audio engine with macOS echo cancellation, so Sommus can be interrupted.

To stop Sommus by talking over it, the mic has to stay open while Sommus speaks — and on laptop
speakers it would hear Sommus itself. macOS voice processing (the echo canceller FaceTime uses)
removes what the Mac is playing from what the mic hears, but only for audio played through the same
AVAudioEngine. So in barge-in mode Kokoro plays through a player node here, and the listener reads
the cleaned mic signal instead of opening its own stream.

Checked silently on the M1 (2026-09-16): voice processing turns on, the engine runs without a run
loop, mic audio keeps arriving during playback. How well the echo is removed can only be judged with
sound on, which is why barge-in is off by default ([voice] barge_in).
"""

from __future__ import annotations

import queue
import threading
from math import gcd

import numpy as np

OUT_RATE = 24_000  # Kokoro's sample rate
MIC_RATE = 16_000  # what the speech detector and Whisper want
CHUNK = 512  # the speech detector's frame
DUCK_LEAST = 10  # AVAudioVoiceProcessingOtherAudioDuckingLevel.min: keep Spotify at its own volume


class MicResampler:
    """Turns the engine's mic buffers (44.1 or 48 kHz) into 16 kHz chunks of 512 samples."""

    def __init__(self, rate: int):
        divisor = gcd(MIC_RATE, rate)
        self.up, self.down = MIC_RATE // divisor, rate // divisor
        self.raw = np.zeros(0, dtype=np.float32)
        self.ready = np.zeros(0, dtype=np.float32)

    def feed(self, samples: np.ndarray) -> list[np.ndarray]:
        from scipy.signal import resample_poly

        self.raw = np.concatenate([self.raw, samples.astype(np.float32)])
        whole = len(self.raw) // self.down * self.down  # convert in exact ratio steps, keep the remainder
        if whole:
            if self.up == self.down:
                converted = self.raw[:whole]
            else:
                converted = resample_poly(self.raw[:whole], self.up, self.down).astype(np.float32)
            self.raw = self.raw[whole:]
            self.ready = np.concatenate([self.ready, converted])
        chunks = []
        while len(self.ready) >= CHUNK:
            chunks.append(self.ready[:CHUNK])
            self.ready = self.ready[CHUNK:]
        return chunks


class DuplexAudio:
    def __init__(self):
        self.mic: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)  # ~13 s of chunks
        self.engine = None
        self.player = None
        self.out_format = None
        self._resampler: MicResampler | None = None

    def start(self) -> None:
        import AVFoundation as AV

        engine = AV.AVAudioEngine.alloc().init()
        mic = engine.inputNode()
        ok, error = mic.setVoiceProcessingEnabled_error_(True, None)
        if not ok:
            raise RuntimeError(f"echo cancellation isn't available: {error}")
        try:  # voice processing turns other apps down by default; Spotify shouldn't dip while listening
            mic.setVoiceProcessingOtherAudioDuckingConfiguration_((False, DUCK_LEAST))
        except Exception:
            pass
        mic_format = mic.outputFormatForBus_(0)
        self._resampler = MicResampler(int(mic_format.sampleRate()))
        mic.installTapOnBus_bufferSize_format_block_(0, 1024, mic_format, self._heard)

        self.player = AV.AVAudioPlayerNode.alloc().init()
        engine.attachNode_(self.player)
        self.out_format = AV.AVAudioFormat.alloc().initStandardFormatWithSampleRate_channels_(float(OUT_RATE), 1)
        engine.connect_to_format_(self.player, engine.mainMixerNode(), self.out_format)
        self.engine = engine
        self._run()

    def _run(self) -> None:
        ok, error = self.engine.startAndReturnError_(None)
        if not ok:
            raise RuntimeError(f"the audio engine didn't start: {error}")
        self.player.play()

    def running(self) -> bool:
        """Changing output device (AirPods connect) stops the engine; the next use restarts it."""
        if self.engine is None:
            return False
        if not self.engine.isRunning():
            self._run()
        return True

    def _heard(self, buffer, when) -> None:  # the audio thread: copy out, nothing slow
        frames = buffer.frameLength()
        if not frames:
            return
        samples = np.frombuffer(buffer.floatChannelData()[0].as_buffer(frames), dtype=np.float32)
        for chunk in self._resampler.feed(samples):
            if self.mic.full():
                self.mic.get_nowait()  # nobody is reading: drop the oldest
            self.mic.put_nowait(chunk)

    def play(self, audio: np.ndarray, stop: threading.Event) -> None:
        """Play 24 kHz mono audio, returning when it has played or as soon as `stop` is set."""
        import AVFoundation as AV

        if not len(audio) or not self.running():
            return
        buffer = AV.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(self.out_format, len(audio))
        buffer.setFrameLength_(len(audio))
        np.frombuffer(buffer.floatChannelData()[0].as_buffer(len(audio)), dtype=np.float32)[:] = audio
        finished = threading.Event()
        self.player.scheduleBuffer_completionHandler_(buffer, finished.set)
        while not finished.wait(0.02):
            if stop.is_set():
                self.player.stop()  # drops what's queued
                self.player.play()  # ready for the next reply
                return

    def stop(self) -> None:
        if self.engine is not None:
            self.engine.inputNode().removeTapOnBus_(0)
            self.engine.stop()
            self.engine = None


class DuplexStream:
    """The echo-cancelled mic, shaped like a sounddevice stream so the Listener can read it."""

    def __init__(self, duplex: DuplexAudio):
        self.duplex = duplex

    def __enter__(self) -> DuplexStream:
        return self

    def __exit__(self, *exc) -> None:
        pass  # the engine keeps running: Sommus is still talking through it

    def read(self, frames: int):
        if not self.duplex.running():
            raise RuntimeError("the audio engine stopped")
        chunk = self.duplex.mic.get(timeout=2)
        return chunk.reshape(-1, 1), False
