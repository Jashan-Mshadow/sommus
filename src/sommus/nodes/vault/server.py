"""Vault node: Jashan's notes in ~/Documents/Jashans_Brain.

Everything is Markdown on disk, so this node is deliberately thin — search, read,
append, and a to-do shortcut, all scoped to the vault so paths stay short and
nothing outside it can be touched from here.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

VAULT = Path(os.environ.get("SOMMUS_VAULT_PATH", "~/Documents/Jashans_Brain")).expanduser()
TODO = "TODO.md"
MAX_NOTE_CHARS = 20_000
SKIP = {".git", "Sources", ".obsidian", "node_modules"}

READ = ToolAnnotations(read_only_hint=True)
REVERSIBLE = ToolAnnotations(read_only_hint=False, destructive_hint=False)

server = MCPServer(
    "sommus-vault",
    instructions="Reads and writes Jashan's Markdown notes vault (courses, TODO, career, projects).",
    log_level="WARNING",
)


class VaultError(Exception):
    """An expected failure worth showing the model."""


def tool(annotations: ToolAnnotations) -> Callable:
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except VaultError as e:
                raise ToolError(str(e)) from e
            except Exception as e:  # never let a tool fail with no explanation
                raise ToolError(f"{type(e).__name__}: {e}") from e

        server.tool(annotations=annotations, structured_output=False)(wrapper)
        return fn

    return decorator


def _resolve(relative: str) -> Path:
    """Keep every path inside the vault, whatever the model passes."""
    candidate = (VAULT / relative.lstrip("/")).expanduser()
    try:
        resolved = candidate.resolve()
        resolved.relative_to(VAULT.resolve())
    except (ValueError, OSError) as e:
        raise VaultError(f"'{relative}' is outside the vault.") from e
    return resolved


def _notes() -> list[Path]:
    return [p for p in sorted(VAULT.rglob("*.md")) if not any(part in SKIP for part in p.relative_to(VAULT).parts)]


@tool(READ)
def search_vault(query: str, limit: int = 12) -> str:
    """Search the notes vault for a word or phrase. Start here when asked about courses, plans or deadlines.

    Args:
        query: Text to look for, e.g. "MATH 115 midterm", "design team", "WARG".
        limit: How many matching lines to return (default 12).
    """
    if not VAULT.is_dir():
        raise VaultError(f"The vault isn't at {VAULT}.")
    hits = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    for note in _notes():
        try:
            lines = note.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            if pattern.search(line):
                hits.append(f"{note.relative_to(VAULT)}:{number}: {line.strip()[:200]}")
                if len(hits) >= limit:
                    return f"Matches for '{query}':\n" + "\n".join(hits) + "\n(more may exist)"
    return f"Matches for '{query}':\n" + "\n".join(hits) if hits else f"Nothing in the vault mentions '{query}'."


@tool(READ)
def read_note(path: str) -> str:
    """Read a note. Paths are relative to the vault, e.g. "TODO.md", "Career/Goals.md".

    Args:
        path: Relative path to the note, or a folder to list.
    """
    note = _resolve(path)
    if note.is_dir():
        return "\n".join(sorted(p.name + ("/" if p.is_dir() else "") for p in note.iterdir()))
    if not note.exists():
        close = [str(p.relative_to(VAULT)) for p in _notes() if path.casefold() in p.name.casefold()][:8]
        raise VaultError(f"No note at '{path}'." + (f" Did you mean: {', '.join(close)}?" if close else ""))
    text = note.read_text(errors="replace")
    return text[:MAX_NOTE_CHARS] + ("\n[truncated]" if len(text) > MAX_NOTE_CHARS else "")


@tool(READ)
def list_notes(folder: str = "") -> str:
    """List the notes in the vault, or in one folder.

    Args:
        folder: Optional folder, e.g. "Career" or "Portfolio/Projects".
    """
    root = _resolve(folder) if folder else VAULT
    notes = [p for p in _notes() if str(p).startswith(str(root))]
    return f"{len(notes)} notes:\n" + "\n".join(str(p.relative_to(VAULT)) for p in notes[:120])


@tool(REVERSIBLE)
def append_to_note(path: str, text: str) -> str:
    """Add lines to the end of a note, creating it if needed.

    Args:
        path: Relative path, e.g. "Portfolio/Projects/Sommus — Build Plan.md".
        text: Markdown to append.
    """
    note = _resolve(path)
    note.parent.mkdir(parents=True, exist_ok=True)
    existing = note.read_text() if note.exists() else ""
    with note.open("a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(text.rstrip() + "\n")
    return f"Added {len(text.splitlines())} line(s) to {path}."


@tool(REVERSIBLE)
def add_todo(text: str) -> str:
    """Add an item to the running to-do list.

    Args:
        text: The task, e.g. "print resumes before the career fair".
    """
    note = _resolve(TODO)
    if not note.exists():
        raise VaultError(f"{TODO} doesn't exist in the vault.")
    with note.open("a") as f:
        f.write(f"\n- [ ] {text.strip()} *(added {datetime.now():%Y-%m-%d})*\n")
    return f"Added to {TODO}: {text.strip()}"


@tool(REVERSIBLE)
def commit_vault(message: str) -> str:
    """Save the vault's changes to git. Do this after writing notes so nothing is lost.

    Args:
        message: Short summary of what changed.
    """
    if not (VAULT / ".git").is_dir():
        raise VaultError("The vault isn't a git repository.")
    status = subprocess.run(["git", "-C", str(VAULT), "status", "--porcelain"], capture_output=True, text=True)
    if not status.stdout.strip():
        return "Nothing to commit — the vault is already up to date."
    for args in (["add", "-A"], ["commit", "-m", message]):
        result = subprocess.run(["git", "-C", str(VAULT), *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise VaultError(f"git {args[0]} failed: {result.stderr.strip()}")
    return f"Committed: {message}"


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
