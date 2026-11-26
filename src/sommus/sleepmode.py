"""Hard sleep: Sommus ignores everything until the wake phrase is sent.

Say the sleep phrase (Telegram, the Mac's keyboard, the iPhone if the transcript is exact) and Sommus goes
deaf and silent on every channel: no replies, no nudges, no morning brief, no reminders, no diary, no model
job, nothing spoken on the Mac. A wrong phrase while asleep gets no answer at all. Only the wake phrase ends
it. The processes keep running (never stop Sommus), they just do nothing.

The phrases are secrets and this repo is public, so they are never in the code, tests or notes. They live in
`.env` (gitignored, next to the API keys): `SOMMUS_SLEEP_PHRASE` / `SOMMUS_WAKE_PHRASE`, set by
`scripts/set-sleep-phrases.sh`. Voice needs plain text there, not a hash: speech-to-text writes an invented word
differently every time ("sleepmax" came back as "sleep mask" and "leakmax"), so the words are compared loosely
(a similarity score) while the digits must match exactly. Older installs may still have a hash
(`SOMMUS_SLEEP_HASH` / `SOMMUS_WAKE_HASH`), which only matches exactly. With neither set the feature is off.
State is one small file in `data/` that survives restarts and deploys, so a restart can't wake it. The Mac keeps
its own copy of the flag (mirrored from the server) so it stays quiet even if the server can't be reached.
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
PHRASE_ENV = {"sleep": "SOMMUS_SLEEP_PHRASE", "wake": "SOMMUS_WAKE_PHRASE"}
HASH_ENV = {"sleep": SLEEP_ENV, "wake": WAKE_ENV}
MIN_SIMILARITY = 0.7  # of the words (digits must match exactly); "leakmax override" vs "sleepmax override" is 0.8
SALT = b"sommus-hard-sleep-v2"
ROUNDS = 50_000
MAX_LENGTH = 80  # a phrase is short: anything longer is not checked (keeps the hashing off normal messages)
CACHE_SECONDS = 1.0  # the Mac's mic loop asks about every audio chunk

# Said out loud, a phrase like "word:thing0517" comes back from speech-to-text as "word thing zero five one seven"
# (or "05 17", "oh five one seven", "517"). So a phrase is reduced to its letters and digits: number words become
# digits, punctuation and spaces go, and leading zeros in a digit run are dropped (0517 = 517).
_NUMBER_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}  # fmt: skip
MAX_SKIPPED_WORDS = 3  # "hey sommus ..." or a misheard name in front of the phrase


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z]+|\d+", text.lower())


# What speech-to-text actually wrote for "max" and "override" (voice.log, 2026-11-25: "sleep mask override ...").
# Applied to the phrase being stored and the phrase heard alike, so it only widens what counts as a match.
_HEARD_AS = (
    ("mask", "max"),
    ("macs", "max"),
    ("macks", "max"),
    ("maks", "max"),
    ("overwrite", "override"),
    ("overrides", "override"),
)


def _canonical(tokens: list[str]) -> str:
    joined = "".join(_NUMBER_WORDS.get(token, token) for token in tokens)
    for wrong, right in _HEARD_AS:
        joined = joined.replace(wrong, right)
    return re.sub(r"(?<!\d)0+(?=\d)", "", joined)


def _hash(canonical: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", canonical.encode(), SALT, ROUNDS).hex()


def digest(phrase: str) -> str:
    return _hash(_canonical(_tokens(phrase)))


def matches(text: str, expected: str) -> bool:
    """Is `text` the phrase whose digest is `expected`? A few words in front of it are allowed."""
    if not expected or not text or len(text) > MAX_LENGTH:
        return False
    expected, tokens = expected.strip().lower(), _tokens(text)
    skips = range(min(MAX_SKIPPED_WORDS, len(tokens) - 1) + 1)
    return any(hmac.compare_digest(_hash(_canonical(tokens[skip:])), expected) for skip in skips)


def _split(phrase_or_text: str) -> tuple[str, str]:
    """(letters, digits) of a phrase after the same clean-up the hash uses."""
    canonical = _canonical(_tokens(phrase_or_text))
    return re.sub(r"\d", "", canonical), re.sub(r"\D", "", canonical)


def _distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def score(text: str, phrase: str) -> tuple[bool, float]:
    """(do the digits match exactly?, how alike are the words 0..1). A few words in front are allowed."""
    if not text or not phrase or len(text) > MAX_LENGTH:
        return False, 0.0
    want_letters, want_digits = _split(phrase)
    tokens, best = _tokens(text), (False, 0.0)
    for skip in range(min(MAX_SKIPPED_WORDS, len(tokens) - 1) + 1):
        letters, digits = _split(" ".join(tokens[skip:]))
        longest = max(len(letters), len(want_letters)) or 1
        best = max(best, (digits == want_digits, 1 - _distance(letters, want_letters) / longest))
    return best


def alike(text: str, phrase: str, other: str = "") -> bool:
    """Is `text` this phrase as speech-to-text might write it: same digits, similar words, and not closer to the
    other phrase (the sleep and wake phrases share most of their words)."""
    digits_ok, similarity = score(text, phrase)
    if not digits_ok or similarity < MIN_SIMILARITY:
        return False
    return not other or score(text, other)[1] <= similarity


class Sleep:
    """The sleep flag for one machine (its `data/sleep.json`) and the phrases that change it."""

    def __init__(self, data_dir: Path | str):
        self.path = Path(data_dir) / "sleep.json"
        self._seen: tuple[float, bool] = (-CACHE_SECONDS, False)
        self.last: tuple[bool, float] = (False, 0.0)

    @property
    def enabled(self) -> bool:
        phrases = all(os.environ.get(name, "").strip() for name in PHRASE_ENV.values())
        return phrases or all(os.environ.get(name, "").strip() for name in HASH_ENV.values())

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

    def which(self, text: str) -> str | None:
        """ "wake" if asleep and `text` is the wake phrase, "sleep" if awake and it is the sleep phrase, else None.
        Changes nothing. `self.last` keeps (digits matched, word similarity) of the last check, for the log."""
        kind = "wake" if self.asleep else "sleep"
        opposite = "sleep" if kind == "wake" else "wake"
        phrase = os.environ.get(PHRASE_ENV[kind], "").strip()
        self.last = score(text, phrase) if phrase else (False, 0.0)
        if phrase:
            other = os.environ.get(PHRASE_ENV[opposite], "").strip()
            return kind if alike(text, phrase, other) else None
        return kind if matches(text, os.environ.get(HASH_ENV[kind], "")) else None

    def command(self, text: str, via: str = "") -> str | None:
        """Apply `text` if it is the phrase that changes the current state. Returns "sleep", "wake" or None.

        Awake, only the sleep phrase counts; asleep, only the wake phrase does. Anything else is not a command
        (and, asleep, gets no answer from whoever called this).
        """
        if not self.enabled or (kind := self.which(text)) is None:
            return None
        self._set(kind == "sleep", via)
        return kind
