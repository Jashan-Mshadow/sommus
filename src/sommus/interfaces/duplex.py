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

import os
import queue
import sys
import threading
import time
from math import gcd
from pathlib import Path

import numpy as np

OUT_RATE = 24_000  # Kokoro's sample rate
MIC_RATE = 16_000  # what the speech detector and Whisper want
CHUNK = 512  # the speech detector's frame
DUCK_LEAST = 10  # AVAudioVoiceProcessingOtherAudioDuckingLevel.min: keep Spotify at its own volume

# Self-healing (2026-11-17). Sommus sat deaf for 6 days with its process alive: the engine stopped delivering mic
# audio (a Bluetooth speaker connecting, a sleep) or a reply never finished playing, and nothing noticed.
BEAT_SECONDS = 5  # how often the watchdog looks
STALL_SECONDS = 8  # no mic audio this long: build the engine again
MAX_REBUILDS = 4  # still nothing after this many fresh engines: exit, and the wrapper starts a clean process
QUEUE_STUCK_SECONDS = 120  # audio "still queued" this long: its completions were lost, so stop waiting for them
PLAY_SLACK_SECONDS = 3.0  # a reply that hasn't finished this long after it should have: the player is dead


def watchdog_action(quiet: float, rebuilds: int) -> str:
    """What the watchdog does when no mic audio has arrived for `quiet` seconds: nothing, a fresh engine, or
    give up so the process restarts (`rebuilds` fresh engines already failed to bring the audio back)."""
    if quiet < STALL_SECONDS:
        return "ok"
    return "exit" if rebuilds >= MAX_REBUILDS else "rebuild"


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
    """Echo cancellation also suppresses quiet sound: measured on this M1 (2026-09-19) the mic came
    through at about half the level of the plain microphone, which makes speech from across the room
    harder to detect. The captured signal is scaled back up before anyone sees it."""

    def __init__(self, gain: float = 2.0, beat_file: Path | None = None, pause_for_media: bool = True):
        self.gain = gain
        self.beat_file = beat_file  # touched while mic audio flows: the wrapper script restarts us if it goes stale
        # While Netflix, YouTube, Spotify... play, echo cancellation is off: it makes the Mac's volume dip and
        # return (2026-11-18). Sommus keeps listening with the plain mic; barge-in needs its name then anyway.
        self.pause_for_media = pause_for_media
        self.voice_processing = True
        self._rebuild_lock = threading.RLock()
        self._media_watching = False
        self.last_frame = time.monotonic()
        self._rebuilt_at = 0.0
        self._watching = False
        self._closed = False
        self.mic: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)  # ~13 s of chunks
        self.engine = None
        self.player = None
        self.out_format = None
        self._resampler: MicResampler | None = None
        self._queued = 0  # buffers scheduled and not yet played (streaming playback)
        self._generation = 0  # bumped by flush(): completions from dropped buffers don't count
        self._count_lock = threading.Lock()

    def start(self) -> None:
        """Order matters. Attaching the player to an engine whose input is *already* in voice-processing
        mode makes the output node fail to initialise (-10875, measured on this M1 2026-09-19 — which is
        why barge-in never started). Connect the player first, then turn echo cancellation on."""
        import AVFoundation as AV

        engine = AV.AVAudioEngine.alloc().init()
        self.player = AV.AVAudioPlayerNode.alloc().init()
        engine.attachNode_(self.player)
        self.out_format = AV.AVAudioFormat.alloc().initStandardFormatWithSampleRate_channels_(float(OUT_RATE), 1)
        engine.connect_to_format_(self.player, engine.mainMixerNode(), self.out_format)

        mic = engine.inputNode()
        if self.voice_processing:
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
        self.engine = engine
        self._closed = False
        self.last_frame = time.monotonic()
        self._run()
        if not self._watching:
            self._watching = True
            threading.Thread(target=self._watch, name="sommus-audio-watchdog", daemon=True).start()
        if self.pause_for_media and not self._media_watching:
            self._media_watching = True
            threading.Thread(target=self._watch_media, name="sommus-media-watch", daemon=True).start()

    def rebuild(self) -> None:
        """A fresh engine, tap and player: after a device change the old tap can stay silent while the engine
        still says it is running."""
        with self._rebuild_lock:
            old, self.engine = self.engine, None
            if old is not None:
                try:
                    old.inputNode().removeTapOnBus_(0)
                    old.stop()
                except Exception:
                    pass
            with self._count_lock:
                self._generation += 1
                self._queued = 0
            self.start()

    def _watch_media(self) -> None:
        """Echo cancellation off while another app plays sound, on again when it stops."""
        from sommus.interfaces import media

        state = media.MediaState()
        failed_rounds = 0
        while not self._closed:
            time.sleep(media.POLL_SECONDS)
            if self.engine is None:
                continue
            if not self.engine.isRunning():
                # Another app starting to play changes the output's format and stops this engine (measured
                # 2026-11-18); waiting for the 8 s no-mic-audio watchdog would leave Sommus deaf meanwhile.
                if self._switch_mode():
                    failed_rounds = 0
                    continue
                # Fresh engines won't start either (2026-11-18: sat deaf for hours rebuilding in a loop, and
                # this thread's rebuilds kept the watchdog below from ever giving up). Same exit as the watchdog.
                failed_rounds += 1
                if failed_rounds >= 2:
                    sys.stderr.write("The audio engine won't come back: restarting Sommus live.\n")
                    os._exit(75)
                continue
            want_vp = not state.feed(media.other_audio_playing())
            if want_vp == self.voice_processing:
                continue
            self.voice_processing = want_vp
            sys.stderr.write(
                "Echo cancellation back on: nothing else is playing.\n"
                if want_vp
                else "Something is playing sound: echo cancellation off so the volume isn't touched.\n"
            )
            self._switch_mode()

    def _switch_mode(self) -> bool:
        """Rebuild for the new mode and check mic audio flows; False if three tries brought nothing. Measured
        2026-11-18: right after echo cancellation the plain mic's engine can come up stopped (its sample rate
        changes under it), and a second build works."""
        for _ in range(3):
            self._rebuilt_at = time.monotonic()
            try:
                self.rebuild()
            except Exception as e:
                sys.stderr.write(f"The audio engine would not restart ({e}).\n")
            time.sleep(1.5)
            if self.engine is not None and self.engine.isRunning() and time.monotonic() - self.last_frame < 1.0:
                return True
        return False

    def _watch(self) -> None:
        rebuilds, queued_for = 0, 0
        while not self._closed:
            time.sleep(BEAT_SECONDS)
            if self.engine is None:
                continue  # stopped on purpose, or being rebuilt right now
            queued_for = queued_for + BEAT_SECONDS if self._queued > 0 else 0
            if queued_for >= QUEUE_STUCK_SECONDS:
                self.flush()
                queued_for = 0
            quiet = time.monotonic() - self.last_frame
            action = watchdog_action(quiet, rebuilds)
            if action == "ok":
                rebuilds = 0
                self._beat()
            elif time.monotonic() - self._rebuilt_at >= STALL_SECONDS:  # give a fresh engine time to deliver
                if action == "exit":
                    sys.stderr.write("No microphone audio, even from a fresh engine: restarting Sommus live.\n")
                    os._exit(75)
                rebuilds += 1
                self._rebuilt_at = time.monotonic()
                sys.stderr.write(f"No microphone audio for {quiet:.0f} s: rebuilding the audio engine.\n")
                try:
                    self.rebuild()
                except Exception as e:
                    sys.stderr.write(f"The audio engine would not restart ({e}).\n")

    def _beat(self) -> None:
        if self.beat_file is not None:
            try:
                self.beat_file.parent.mkdir(parents=True, exist_ok=True)
                self.beat_file.touch()
            except OSError:
                pass

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
        self.last_frame = time.monotonic()
        samples = np.frombuffer(buffer.floatChannelData()[0].as_buffer(frames), dtype=np.float32)
        gain = self.gain if self.voice_processing else 1.0  # the plain mic isn't halved like the echo-cancelled one
        if gain != 1.0:
            samples = np.clip(samples * gain, -1.0, 1.0)
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
        give_up = time.monotonic() + len(audio) / OUT_RATE + PLAY_SLACK_SECONDS
        while not finished.wait(0.02):
            if stop.is_set() or time.monotonic() > give_up:
                # `stop`, or a player that died (the output device changed): its completion never comes, and
                # waiting for it here kept the listener deaf for days (2026-11-11).
                self.player.stop()  # drops what's queued
                self.player.play()  # ready for the next reply
                return

    def _buffer(self, audio: np.ndarray):
        import AVFoundation as AV

        buffer = AV.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(self.out_format, len(audio))
        buffer.setFrameLength_(len(audio))
        np.frombuffer(buffer.floatChannelData()[0].as_buffer(len(audio)), dtype=np.float32)[:] = audio
        return buffer

    def enqueue(self, audio: np.ndarray) -> None:
        """Queue 24 kHz mono audio behind whatever is playing and return at once — for speech that arrives in
        pieces over a network (Gemini Live), where waiting for each piece to play would leave gaps."""
        if not len(audio) or not self.running():
            return
        with self._count_lock:
            self._queued += 1
            generation = self._generation

        def played() -> None:  # an audio thread
            with self._count_lock:
                if generation == self._generation:
                    self._queued = max(0, self._queued - 1)

        self.player.scheduleBuffer_completionHandler_(self._buffer(audio), played)

    @property
    def speaking(self) -> bool:
        return self._queued > 0

    def flush(self) -> None:
        """Drop everything queued, now: he started talking over it."""
        with self._count_lock:
            self._generation += 1
            self._queued = 0
        if self.player is not None:
            self.player.stop()
            self.player.play()

    def stop(self) -> None:
        self._closed = True
        if self.engine is not None:
            self.engine.inputNode().removeTapOnBus_(0)
            self.engine.stop()
            self.engine = None


class DuplexStream:
    """The echo-cancelled mic, shaped like a sounddevice stream so the Listener can read it."""

    def __init__(self, duplex: DuplexAudio):
        self.duplex = duplex

    def __enter__(self) -> DuplexStream:
        # The engine kept capturing while the listener was away (a chime playing): that audio is old now,
        # and reading it would put the listener seconds behind the room.
        while True:
            try:
                self.duplex.mic.get_nowait()
            except queue.Empty:
                return self

    def __exit__(self, *exc) -> None:
        pass  # the engine keeps running: Sommus is still talking through it

    def read(self, frames: int):
        if not self.duplex.running():
            raise RuntimeError("the audio engine stopped")
        chunk = self.duplex.mic.get(timeout=2)
        return chunk.reshape(-1, 1), False
