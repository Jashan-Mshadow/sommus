"""Is something else on the Mac playing sound (Netflix, YouTube, Spotify, a video file)?

Why: macOS echo cancellation (voice processing, used for barge-in) takes over the audio route. While a video
plays it makes the Mac's output volume dip and come back (2026-11-18, Jashan's Bluetooth speaker). So barge-in
steps aside while other audio plays and comes back when it stops.

How: coreaudiod holds a power assertion for every process that is outputting sound — `pmset -g assertions`
lists it with `Resources: audio-out <device>` and `Created for PID: <pid>`. That works for any app, with no
permissions. Sommus's own audio is left out, and so is anything short (a chime): see `MediaState`.
"""

from __future__ import annotations

import os
import re
import subprocess

POLL_SECONDS = 2.0  # how often the watcher looks
ON_AFTER = 2  # polls in a row with sound before it counts as media (~4 s: a notification ding doesn't)
OFF_AFTER = 5  # polls in a row of silence before it is over (~10 s: a pause or a quiet scene doesn't flip it back)


def audio_out_pids(assertions: str) -> set[int]:
    """The processes outputting sound, read from `pmset -g assertions` text."""
    pids: set[int] = set()
    created_for: int | None = None
    for line in assertions.splitlines():
        match = re.search(r"Created for PID:\s*(\d+)", line)
        if match:
            created_for = int(match.group(1))
            continue
        # Output only. A process holding the mic and the speaker together is not a video: corespeechd ("Hey Siri")
        # sits there all day, Sommus's own engine does, and a call does. Counting them kept echo cancellation off
        # for good and Sommus heard itself (2026-11-18).
        if "Resources:" in line and "audio-out" in line and "audio-in" not in line and created_for is not None:
            pids.add(created_for)
        if "Resources:" in line:
            created_for = None
    return pids


def _command(pid: int) -> str:
    try:
        done = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=3)
        return done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def other_audio_playing() -> bool:
    """True if a process that is not Sommus is outputting sound right now."""
    try:
        done = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    for pid in audio_out_pids(done.stdout):
        if pid == os.getpid():
            continue
        if "sommus" in _command(pid).lower():  # its own voice, the Mac node's `say`, the bridge
            continue
        return True
    return False


class MediaState:
    """Smooths the on/off readings so a chime doesn't switch modes and a pause doesn't switch back."""

    def __init__(self, on_after: int = ON_AFTER, off_after: int = OFF_AFTER):
        self.on_after, self.off_after = on_after, off_after
        self.playing = False
        self._run = 0  # polls in a row that disagree with `playing`

    def feed(self, sound_now: bool) -> bool:
        """Add one reading; returns whether media counts as playing now."""
        if sound_now == self.playing:
            self._run = 0
            return self.playing
        self._run += 1
        if self._run >= (self.on_after if sound_now else self.off_after):
            self.playing, self._run = sound_now, 0
        return self.playing
