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

from sommus.brain import memory

VAULT = Path(os.environ.get("SOMMUS_VAULT_PATH", "~/Documents/Jashans_Brain")).expanduser()
TODO = "TODO.md"
MEMORY = Path(os.environ.get("SOMMUS_MEMORY_PATH", str(VAULT / "Sommus Memory.md"))).expanduser()
MAX_NOTE_CHARS = 20_000
# Longer notes return an outline first. read_note averaged 8.7k chars per call in the log — the
# most expensive read Sommus makes — and most questions only need one section of the note.
LONG_NOTE_CHARS = 6_000
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
        heads = None
        for number, line in enumerate(lines, 1):
            if pattern.search(line):
                heads = heads if heads is not None else _headings(lines)
                under = next((title for i, _, title in reversed(heads) if i < number - 1), "")
                where = f" [{under[:60]}]" if under else ""
                hits.append(f"{note.relative_to(VAULT)}:{number}{where}: {line.strip()[:200]}")
                if len(hits) >= limit:
                    return f"Matches for '{query}':\n" + "\n".join(hits) + "\n(more may exist)"
    return f"Matches for '{query}':\n" + "\n".join(hits) if hits else f"Nothing in the vault mentions '{query}'."


HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, title) for every Markdown heading outside code fences."""
    found, fenced = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and (m := HEADING.match(line)):
            found.append((i, len(m.group(1)), m.group(2)))
    return found


def _outline(path: str, text: str) -> str:
    """A long note's table of contents plus its opening, instead of the whole thing."""
    lines = text.splitlines()
    heads = _headings(lines)
    ends = [start for start, _, _ in heads[1:]] + [len(lines)]
    toc = [
        f"{'  ' * (level - 1)}- {title}  ({sum(len(line) + 1 for line in lines[start:end]) / 1000:.1f}k chars)"
        for (start, level, title), end in zip(heads, ends, strict=True)
    ]
    intro = "\n".join(lines[: heads[0][0]] if heads else lines).strip()[:1500]
    return (
        f"{path} is long ({len(text):,} chars), so here is its outline. Call read_note again with "
        f'section="<heading words>" to read one part, or section="all" for everything.\n\n'
        + (intro + "\n\n" if intro else "")
        + "\n".join(toc)
    )


def _section(path: str, text: str, section: str) -> str:
    """The first heading containing `section` (any case) and everything under it, down to the next
    heading of the same or a higher level."""
    lines = text.splitlines()
    heads = _headings(lines)
    wanted = section.casefold().strip()
    matches = [h for h in heads if wanted in h[2].casefold()]
    if not matches:
        titles = ", ".join(title for _, _, title in heads[:40])
        raise VaultError(f"No heading in {path} contains '{section}'. Headings: {titles}")
    start, level, _ = matches[0]
    end = next((i for i, lvl, _ in heads if i > start and lvl <= level), len(lines))
    body = "\n".join(lines[start:end]).strip()
    others = [title for _, _, title in matches[1:6]]
    note = f"\n\n(Other headings matching '{section}': {', '.join(others)})" if others else ""
    return body[:MAX_NOTE_CHARS] + ("\n[truncated]" if len(body) > MAX_NOTE_CHARS else "") + note


@tool(READ)
def read_note(path: str, section: str = "") -> str:
    """Read a note. Paths are relative to the vault, e.g. "TODO.md", "Career/Goals.md".

    Long notes come back as an outline first; then read just the part you need with `section`.

    Args:
        path: Relative path to the note, or a folder to list.
        section: Words from a heading, e.g. "Weekend" or "MATH 115", to read only that part.
            "all" returns the whole note. Leave empty for short notes or to get a long note's outline.
    """
    note = _resolve(path)
    if note.is_dir():
        return "\n".join(sorted(p.name + ("/" if p.is_dir() else "") for p in note.iterdir()))
    if not note.exists():
        close = [str(p.relative_to(VAULT)) for p in _notes() if path.casefold() in p.name.casefold()][:8]
        raise VaultError(f"No note at '{path}'." + (f" Did you mean: {', '.join(close)}?" if close else ""))
    text = note.read_text(errors="replace")
    if section.strip() and section.strip().casefold() != "all":
        return _section(path, text, section)
    if not section.strip() and len(text) > LONG_NOTE_CHARS and _headings(text.splitlines()):
        return _outline(path, text)
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


@tool(REVERSIBLE)
def remember(fact: str) -> str:
    """Save a lasting fact about Jashan so it's known in every future conversation.

    Args:
        fact: One short sentence, e.g. "Gym days are Monday, Wednesday and Friday at 7 AM".
    """
    try:
        return memory.remember(MEMORY, fact)
    except memory.MemoryError as e:
        raise VaultError(str(e)) from e


@tool(REVERSIBLE)
def forget(words: str) -> str:
    """Remove a remembered fact that's wrong or out of date.

    Args:
        words: Words from the fact, e.g. "gym days".
    """
    try:
        return memory.forget(MEMORY, words)
    except memory.MemoryError as e:
        raise VaultError(str(e)) from e


@tool(READ)
def list_memories() -> str:
    """Everything Jashan has asked to be remembered."""
    known = memory.facts(MEMORY)
    return "\n".join(f"- {f}" for f in known) if known else "Nothing remembered yet."


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
