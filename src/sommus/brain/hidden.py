"""Invisible Unicode that can hide a sentence from a person but not from the model.

Tag characters (U+E0000-E007F) render as nothing yet spell out text a model can read, and bidi controls make
text draw differently from what it says. Pattern from mattabate/life-distributable (add-0062).
"""

from __future__ import annotations

import re

HIDDEN = re.compile("[\U000e0000-\U000e007f​-‏‪-‮⁦-⁩⁠﻿]")


def strip(text: str) -> tuple[str, int]:
    """The text without hidden characters, and how many were removed."""
    return HIDDEN.subn("", text)


def in_args(value: object) -> bool:
    """True if any string inside a tool's arguments holds a hidden character."""
    if isinstance(value, str):
        return bool(HIDDEN.search(value))
    if isinstance(value, dict):
        return any(in_args(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(in_args(v) for v in value)
    return False
