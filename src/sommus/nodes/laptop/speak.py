"""Sommus saying something out loud on the Mac without being asked: the server's nudges and reminders.

The voice is the one `sommus voice` uses (Kokoro, else macOS `say`), loaded the first time something is said.
It won't speak when the Mac is muted or turned all the way down, or while the quiet flag is set (`set_quiet`);
the server then sends the line to Telegram instead. Speaking happens on a thread of its own: the tool returns
at once, so `sommus live` (which serves this node in its own event loop) never stalls while a line plays.
"""

from __future__ import annotations

import threading
from pathlib import Path

from sommus import config
from sommus.nodes.laptop import macos

QUIET_FLAG = config.ROOT / "data" / "quiet"
MAX_CHARS = 400  # a nudge, not a reading: anything longer belongs on the screen


class NotSpoken(macos.ActionError):
    """Why a line wasn't said ("Didn't say it: the Mac is muted."); the server falls back to Telegram."""

    def __init__(self, reason: str):
        super().__init__(f"Didn't say it: {reason}.")


class Announcer:
    def __init__(self, flag: Path = QUIET_FLAG):
        self.flag = flag
        self.voice = None
        self._lock = threading.Lock()  # one line at a time: two nudges at once would talk over each other
        self._settings: dict | None = None

    @property
    def settings(self) -> dict:
        if self._settings is None:
            self._settings = config.section("voice")
        return self._settings

    # -- quiet

    def quiet(self) -> bool:
        return self.flag.exists()

    def set_quiet(self, on: bool) -> None:
        if on:
            self.flag.parent.mkdir(parents=True, exist_ok=True)
            self.flag.touch()
        else:
            self.flag.unlink(missing_ok=True)

    def why_not(self) -> str:
        """'' when it's fine to speak, else the reason not to."""
        if self.quiet():
            return "quiet mode is on"
        state = macos.volume()
        if state.muted:
            return "the Mac is muted"
        if state.output == 0:
            return "the Mac's volume is at zero"
        return ""

    # -- speaking

    def line(self, text: str) -> str:
        from sommus.interfaces.voice import clean_for_speech

        return clean_for_speech(text, self.settings.get("say_as", {}))[:MAX_CHARS]

    def announce(self, text: str) -> str:
        spoken = self.line(text)
        if not spoken:
            raise NotSpoken("nothing to say")
        if reason := self.why_not():
            raise NotSpoken(reason)
        threading.Thread(target=self._speak, args=(spoken,), daemon=True, name="announce").start()
        return spoken

    def _speak(self, sentence: str) -> None:
        with self._lock:
            stop = threading.Event()
            try:
                voice = self._voice()
                voice.play(voice.synthesize(sentence), stop)
                voice.rest(stop)
            except Exception:  # Kokoro failed to load or play: the built-in voice always works
                self.voice = self._say()
                self.voice.play(sentence, stop)

    def _voice(self):
        if self.voice is None:
            from sommus.interfaces.voice import make_voice

            self.voice = make_voice(self.settings)
        return self.voice

    def _say(self):
        from sommus.interfaces.voice import SayVoice

        return SayVoice(self.settings.get("say_voice", "Samantha"), int(self.settings.get("say_rate", 190)))


announcer = Announcer()
