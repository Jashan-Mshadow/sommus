"""The PIN gate: personal actions wait for Jashan's PIN.

Sommus has full permission, and it listens to a microphone. Until it can recognise his voice,
anything personal — email, messages, files, notes, the shell, Claude Code with his accounts —
needs the PIN first. Basic things (volume, music, weather, apps) never ask.

Enforced here in code, not in the prompt: a locked tool never runs, whatever the model decides.
The PIN is said or typed as its own message; it never reaches the model, the conversation or the
log, and only a salted scrypt hash is stored (data/pin.json, gitignored). One unlock covers every
interface for a few minutes, and three wrong tries lock it out for five.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path

PIN_FILE = "pin.json"
MAX_TRIES = 3
LOCKOUT_SECONDS = 300
# A PIN said with a pause in it arrives as two utterances ("268", then "4"): hold the first briefly.
PARTIAL_SECONDS = 12

ONES = {
    "zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "to": 2, "too": 2, "three": 3, "four": 4, "for": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
}  # fmt: skip
TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}  # fmt: skip
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
# Said around a PIN but not part of it: "my pin is 2684", "override 2684", "unlock, 2684".
AROUND = {"my", "pin", "is", "its", "it", "the", "code", "passcode", "password", "override", "unlock", "sommus", "hey",
          "ok", "okay", "number"}  # fmt: skip


def spoken_digits(text: str, least: int = 4) -> str | None:
    """The digits of a message that is nothing but digits: '2684', '26 84', 'two six eight four',
    'twenty six eighty four', 'my pin is 2684'. None for anything else."""
    tokens = re.findall(r"[a-z]+|\d+", text.lower().replace("-", " "))
    tokens = [t for t in tokens if t not in AROUND and t != "and"]
    digits, i = "", 0
    while i < len(tokens):
        token = tokens[i]
        if token.isdigit():
            digits += token
        elif token in TENS:
            following = tokens[i + 1] if i + 1 < len(tokens) else ""
            if following in ONES and ONES[following] != 0:
                digits += str(TENS[token] + ONES[following])
                i += 1
            else:
                digits += str(TENS[token])
        elif token in TEENS:
            digits += str(TEENS[token])
        elif token in ONES:
            digits += str(ONES[token])
        else:
            return None
        i += 1
    return digits if least <= len(digits) <= 8 else None


# A PIN said inside a request: "the PIN is 2684, send Didi a message", "override 2684 read my email".
INLINE = re.compile(
    r"\b(?:my\s+|the\s+)?(?:pin|passcode|password|override|code)(?:\s+(?:number|is|was))*[\s:,]*((?:\d[\s-]?){4,8})\b[.,]?",
    re.I,
)


def split_pin(text: str) -> tuple[str | None, str]:
    """('2684', 'Send message to Didi.') from 'The PIN is 2684. Send message to Didi.' — the request
    goes on without the PIN in it. (None, text) when there's no PIN."""
    whole = spoken_digits(text, least=1)  # a lone "4" can be the tail of a PIN said with a pause
    if whole:
        return whole, ""
    found = INLINE.search(text)
    if not found:
        return None, text
    rest = (text[: found.start()] + " " + text[found.end() :]).strip(" ,.;:")
    return re.sub(r"\D", "", found.group(1)), re.sub(r"\s{2,}", " ", rest)


def redact(text: str) -> str:
    """For screens, logs and history files."""
    digits, rest = split_pin(text)
    if digits is None:
        return text
    return f"•••• {rest}".strip()


def _hash(pin: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1)


def set_pin(data_dir: Path, pin: str) -> None:
    if not re.fullmatch(r"\d{4,8}", pin):
        raise ValueError("The PIN must be 4 to 8 digits.")
    salt = secrets.token_bytes(16)
    path = data_dir / PIN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"salt": salt.hex(), "hash": _hash(pin, salt).hex()}))
    path.chmod(0o600)


class Gate:
    def __init__(
        self,
        data_dir: Path,
        tools: set[str] | frozenset[str],
        unlock_minutes: float = 10,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.path = data_dir / PIN_FILE
        self.tools = frozenset(tools)
        self.unlock_seconds = unlock_minutes * 60
        self.clock = clock
        self.unlocked_until = 0.0
        self.wrong = 0
        self.locked_out_until = 0.0
        self.pending: str | None = None  # the request that hit the lock, replayed once unlocked
        self.partial = ("", 0.0)  # digits heard so far, when a PIN is said in pieces

    @property
    def active(self) -> bool:
        return bool(self.tools)

    @property
    def pin_set(self) -> bool:
        return self.path.exists()

    @property
    def unlocked(self) -> bool:
        return self.clock() < self.unlocked_until

    def needs_pin(self, tool: str) -> bool:
        return tool in self.tools and not self.unlocked

    def locked_message(self, user: str) -> str:
        if not self.pin_set:
            return f"Locked: personal actions need a PIN, and none is set yet. Tell {user} to run `sommus pin`."
        return (
            f"Locked: this is a personal action and needs {user}'s PIN. Ask him for it in a few words and stop — "
            "don't try another tool for the same thing. Once he gives it, the request runs by itself."
        )

    def lock(self) -> None:
        self.unlocked_until = 0.0

    def attempt(self, text: str) -> str | None:
        """None if the message isn't a PIN. '' if it's the start of one and more digits are expected.
        Otherwise the spoken reply: unlocked, wrong, or locked out."""
        if not self.active:
            return None
        pin = spoken_digits(text, least=1 if not self.unlocked else 4)
        if pin is None:
            return None
        held, at = self.partial
        if held and self.clock() - at <= PARTIAL_SECONDS:
            pin = (held + pin)[:8]
        if len(pin) < 4:
            self.partial = (pin, self.clock())
            return ""  # wait for the rest, without a word: he's mid-PIN
        self.partial = ("", 0.0)
        return self.check(pin)

    def check(self, pin: str) -> str:
        now = self.clock()
        if now < self.locked_out_until:
            return f"Too many wrong tries. Try again in {int(self.locked_out_until - now) // 60 + 1} minutes."
        if not self.pin_set:
            return "No PIN is set yet. Run sommus pin in the terminal."
        stored = json.loads(self.path.read_text())
        if hmac.compare_digest(_hash(pin, bytes.fromhex(stored["salt"])), bytes.fromhex(stored["hash"])):
            self.wrong = 0
            self.unlocked_until = now + self.unlock_seconds
            return f"Unlocked for {int(self.unlock_seconds // 60)} minutes."
        self.wrong += 1
        heard = f"Wrong PIN — I heard {len(pin)} digits."
        if self.wrong >= MAX_TRIES:
            self.wrong = 0
            self.pending = None
            self.locked_out_until = now + LOCKOUT_SECONDS
            return f"{heard} Locked for 5 minutes."
        return heard
