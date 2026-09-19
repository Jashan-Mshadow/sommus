"""Long-term memory: short facts Jashan tells Sommus, kept across conversations.

`profile.md` holds what's known up front and is edited by hand. This file is written by Sommus
itself ("remember my gym days are Mon/Wed/Fri") and loaded into the cached system prompt, so a
fact costs a few tokens per request and nothing to recall. It lives in the private vault: backed
up with git, readable by Claude Code, never in the public repo.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

HEADER = (
    "# Sommus Memory\n\n"
    "Facts Jashan told Sommus to keep. Sommus writes this file (remember / forget); "
    "edit freely, one fact per line.\n\n"
)
FACT = re.compile(r"^- (.+?)(?:\s+\*\((\d{4}-\d{2}-\d{2})\)\*)?\s*$")
MAX_FACTS = 80
MAX_FACT_CHARS = 300
# Memory sits in every prompt and in a git repo: never a place for secrets.
SECRET = re.compile(r"\b(password|passcode|passphrase|pin|api[ _-]?key|token|secret|2fa|otp|security code)\b", re.I)


class MemoryError(Exception):
    """A fact that can't be stored, with a reason worth telling the model."""


def facts(path: Path) -> list[str]:
    if not path.exists():
        return []
    found = []
    for line in path.read_text(errors="replace").splitlines():
        if m := FACT.match(line):
            found.append(m.group(1).strip())
    return found


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "".join(f"{line}\n" for line in lines))


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(errors="replace").splitlines() if FACT.match(line)]


def remember(path: Path, fact: str, today: date | None = None) -> str:
    fact = " ".join(fact.split()).rstrip(".")
    if not fact:
        raise MemoryError("Nothing to remember.")
    if len(fact) > MAX_FACT_CHARS:
        raise MemoryError(f"Keep it to one short sentence (under {MAX_FACT_CHARS} characters).")
    if SECRET.search(fact):
        raise MemoryError("That looks like a secret (password, PIN, key or code). Those are never stored.")
    lines = _lines(path)
    known = [f.casefold() for f in facts(path)]
    if fact.casefold() in known:
        return f"Already remembered: {fact}."
    if len(lines) >= MAX_FACTS:
        raise MemoryError(f"Memory is full ({MAX_FACTS} facts). Forget an outdated one first.")
    lines.append(f"- {fact} *({(today or date.today()).isoformat()})*")
    _write(path, lines)
    return f"Remembered: {fact}."


def forget(path: Path, words: str) -> str:
    """Remove every fact containing all of these words (any case)."""
    wanted = [w for w in re.findall(r"\w+", words.casefold()) if w]
    if not wanted:
        raise MemoryError("Say which fact to forget.")
    lines = _lines(path)
    keep, gone = [], []
    for line in lines:
        text = FACT.match(line).group(1)
        (gone if all(w in text.casefold() for w in wanted) else keep).append(line)
    if not gone:
        raise MemoryError(f"No remembered fact mentions '{words}'.")
    if len(gone) > 3:
        raise MemoryError(f"'{words}' matches {len(gone)} facts; be more specific.")
    _write(path, keep)
    return "Forgot: " + "; ".join(FACT.match(line).group(1) for line in gone) + "."


def prompt_block(path: Path | None, user: str) -> str:
    known = facts(path) if path else []
    if not known:
        return ""
    return f"\n\n--- What {user} has told you to remember ---\n" + "\n".join(f"- {f}" for f in known)
