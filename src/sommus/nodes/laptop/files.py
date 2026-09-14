"""Clipboard, Spotlight search, and reading or appending text files."""

from __future__ import annotations

import subprocess
from pathlib import Path

from sommus.nodes.laptop.macos import ActionError, _run

MAX_READ_CHARS = 20_000
SKIP_PARTS = {"node_modules", ".venv", "venv", ".git", "Library", "site-packages", "dist", "build", ".cache"}


def clipboard_get() -> str:
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout


def clipboard_set(text: str) -> None:
    proc = subprocess.run(["pbcopy"], input=text, text=True, capture_output=True)
    if proc.returncode != 0:
        raise ActionError("Couldn't write to the clipboard.")


def _interesting(path: Path) -> bool:
    return not any(part in SKIP_PARTS for part in path.parts)


def find_files(query: str, limit: int = 10, folder: str | None = None) -> list[Path]:
    """Spotlight search by file name, newest first, skipping dependency folders."""
    root = Path(folder).expanduser() if folder else Path.home()
    if not root.is_dir():
        raise ActionError(f"'{root}' isn't a folder.")
    output = _run(["mdfind", "-onlyin", str(root), "-name", query], timeout=20)
    paths = [Path(line) for line in output.splitlines() if line]
    found = [p for p in paths if _interesting(p) and p.exists()]
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[:limit]


def resolve(path: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise ActionError(f"Nothing exists at '{path}'.")
    return resolved


def read_text_file(path: str, max_chars: int = MAX_READ_CHARS) -> str:
    resolved = resolve(path)
    if resolved.is_dir():
        return "\n".join(sorted(p.name + ("/" if p.is_dir() else "") for p in resolved.iterdir()))
    try:
        text = resolved.read_text(errors="replace")
    except OSError as e:
        raise ActionError(f"Couldn't read '{path}': {e}") from e
    return text[:max_chars] + ("\n[truncated]" if len(text) > max_chars else "")


def append_text_file(path: str, text: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.parent.is_dir():
        raise ActionError(f"The folder '{resolved.parent}' doesn't exist.")
    with resolved.open("a") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    return resolved


MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


def download_url(url: str, folder: str = "~/Downloads", filename: str | None = None) -> Path:
    """Fetch a URL straight to disk. Public URLs only — pages behind a login need the browser.

    Uses curl, not Python's urllib: this Python build ships no CA certificates, so every
    HTTPS request would fail certificate verification. curl uses macOS's own trust store.
    """
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ActionError("Only http:// and https:// URLs can be downloaded.")
    target_dir = Path(folder).expanduser()
    if not target_dir.is_dir():
        raise ActionError(f"'{folder}' isn't a folder.")
    target = target_dir / (filename or unquote(Path(parsed.path).name) or "download")
    try:
        _run(
            [
                "curl",
                "-fsSL",
                "--max-time",
                "90",
                "--max-filesize",
                str(MAX_DOWNLOAD_BYTES),
                "-A",
                "Sommus/1.0",
                "-o",
                str(target),
                url,
            ],
            timeout=100,
        )
    except ActionError as e:
        message = str(e)
        if "401" in message or "403" in message:
            raise ActionError(
                f"The server refused it ({message.strip()}). It's probably behind a login, which only "
                "the browser has — open the page in Chrome and save it from there."
            ) from e
        target.unlink(missing_ok=True)
        raise ActionError(f"Download failed: {message.strip()}") from e
    head = target.read_bytes()[:64].lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        target.unlink(missing_ok=True)
        raise ActionError(
            "That URL returned a web page, not a file — usually a login wall. Open it in the browser instead."
        )
    return target


def open_path(path: str) -> Path:
    resolved = resolve(path)
    _run(["open", str(resolved)])
    return resolved
