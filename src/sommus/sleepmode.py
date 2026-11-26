"""Hard sleep: Sommus ignores everything until the wake phrase is sent.

Say the sleep phrase (Telegram, the Mac's keyboard, the iPhone if the transcript is exact) and Sommus goes
deaf and silent on every channel: no replies, no nudges, no morning brief, no reminders, no diary, no model
job, nothing spoken on the Mac. A wrong phrase while asleep gets no answer at all. Only the wake phrase ends
it. The processes keep running (never stop Sommus), they just do nothing.

The phrases are secrets and this repo is public, so they are never in the code. `.env` holds only a hash of
each (`SOMMUS_SLEEP_HASH`, `SOMMUS_WAKE_HASH`); `sommus sleep-hash` makes the line to paste. With no hashes set
the feature is off. State is one small file in `data/` that survives restarts and deploys, so a restart can't
wake it. The Mac keeps its own copy of the flag (mirrored from the server) so it stays quiet even if the
server can't be reached.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

SLEEP_ENV, WAKE_ENV = "SOMMUS_SLEEP_HASH", "SOMMUS_WAKE_HASH"
SALT = b"sommus-hard-sleep-v1"
ROUNDS = 50_000
MAX_LENGTH = 80  # a phrase is short: anything longer is not checked (keeps the hashing off normal messages)
CACHE_SECONDS = 1.0  # the Mac's mic loop asks about every audio chunk

_NAME = re.compile(r"^(?:hey[\s,]+)?sommus\b[\s,:;.\-]*")


def normalize(text: str) -> str:
    """Lower case, no spaces, no leading "Sommus"/"Hey Sommus", no trailing punctuation."""
    cleaned = _NAME.sub("", text.strip().lower())
    return re.sub(r"\s+", "", cleaned).rstrip(".!?,;")


def digest(phrase: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", normalize(phrase).encode(), SALT, ROUNDS).hex()


def matches(text: str, expected: str) -> bool:
    if not expected or not text or len(text) > MAX_LENGTH:
        return False
    return hmac.compare_digest(digest(text), expected.strip().lower())


class Sleep:
    """The sleep flag for one machine (its `data/sleep.json`) and the phrases that change it."""

    def __init__(self, data_dir: Path | str):
        self.path = Path(data_dir) / "sleep.json"
        self._seen: tuple[float, bool] = (-CACHE_SECONDS, False)

    @property
    def enabled(self) -> bool:
        return bool(os.environ.get(SLEEP_ENV, "").strip() and os.environ.get(WAKE_ENV, "").strip())

    @property
    def asleep(self) -> bool:
        now = time.monotonic()
        if now - self._seen[0] < CACHE_SECONDS:
            return self._seen[1]
        asleep = self.path.exists()
        self._seen = (now, asleep)
        return asleep

    def _set(self, asleep: bool, via: str = "") -> None:
        if asleep:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            since = datetime.now().isoformat(timespec="seconds")
            temp.write_text(json.dumps({"asleep": True, "since": since, "via": via}))
            temp.replace(self.path)
        else:
            self.path.unlink(missing_ok=True)
        self._seen = (time.monotonic(), asleep)

    def mirror(self, asleep: bool) -> None:
        """The Mac copies the server's state, so the flag is there if the server is later unreachable."""
        if asleep != self.asleep:
            self._set(asleep, "server")

    def command(self, text: str, via: str = "") -> str | None:
        """Apply `text` if it is the phrase that changes the current state. Returns "sleep", "wake" or None.

        Awake, only the sleep phrase counts; asleep, only the wake phrase does. Anything else is not a command
        (and, asleep, gets no answer from whoever called this).
        """
        if not self.enabled:
            return None
        if self.asleep:
            if matches(text, os.environ.get(WAKE_ENV, "")):
                self._set(False)
                return "wake"
        elif matches(text, os.environ.get(SLEEP_ENV, "")):
            self._set(True, via)
            return "sleep"
        return None
