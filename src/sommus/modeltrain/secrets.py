"""Anything that looks like a secret is kept out of the training data: PINs, passwords, tokens, long numbers."""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"\bpin\b\W{0,12}(is\W{0,4})?\d{3,8}\b", re.I),  # "pin 4821", "my pin is 4821"
    re.compile(r"\b\d{4,8}\W{0,8}\bis\W{0,4}(my\W{0,4})?pin\b", re.I),
    re.compile(r"\b(password|passcode|passwd|pwd|secret|api[ _-]?key|token)\b\W{0,10}(is|=|:)\W{0,4}\S{3,}", re.I),
    re.compile(r"\d{8,}"),  # a long run of digits: card, account, phone, id
    re.compile(r"\b\d(?:[ -]?\d){11,}\b"),  # the same, written with spaces or dashes
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),  # API keys
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\b(?=[A-Za-z0-9+/_=-]*\d)(?=[A-Za-z0-9+/_=-]*[A-Za-z])[A-Za-z0-9+/_=-]{32,}"),  # long mixed tokens
]


def looks_secret(value: object) -> bool:
    return any(p.search(text) for text in _strings(value) for p in _PATTERNS)


def redact(text: str) -> str:
    for p in _PATTERNS:
        text = p.sub("[redacted]", text)
    return text


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)
    elif value is not None:
        yield str(value)
