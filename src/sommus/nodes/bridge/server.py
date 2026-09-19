"""AI bridge node: Gemini and ChatGPT (Codex) on Jashan's own free sign-ins, as tools.

One MCP server, two clients: Sommus loads it as a node, and Claude Code loads it too
(`claude mcp add --scope user ai-bridge ...`), so both can hand work to another model.

What each is for:
- `gemini_search` — a web question answered with Google Search. $0 on a free AI Studio key
  (~250 requests a day on Flash), instead of ~2.5¢ through the API.
- `ask_gemini` — heavy reading: long PDFs (scanned ones too), several files, long research. It
  reads everything and sends back a summary, so the caller's context gets a page, not the book.
- `ask_gpt` — a second opinion or a different model on a hard problem, through Codex on the
  ChatGPT sign-in (the free plan's allowance is small; save it for things that need it).

Both run read-only: Gemini in plan mode (reads and searches, never edits or runs commands),
Codex in its read-only sandbox. Each call starts in an empty folder; a file is visible only when
it's passed in `files`. Whatever is sent goes to Google or OpenAI, whose free plans may use it to
improve their models — so callers send what the task needs, not the vault by default.
"""

from __future__ import annotations

import functools
import glob
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
GEMINI_MODEL = os.environ.get("SOMMUS_GEMINI_MODEL", "")  # empty: the CLI's default
CODEX_MODEL = os.environ.get("SOMMUS_CODEX_MODEL", "")  # empty: ~/.codex/config.toml (gpt-5.5 today)
SEARCH_TIMEOUT, READ_TIMEOUT, GPT_TIMEOUT = 90, 600, 900
MAX_REPLY_CHARS = 12_000
# Keys that would move a CLI onto paid billing. Gemini is the exception: Google ended free Gemini CLI
# sign-in for individuals on 2026-06-18, so Gemini runs on a free AI Studio key kept in ~/.gemini/.env
# (the CLI reads it itself). That key is free as long as billing is never enabled on its Google project.
BILLING_VARS = ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "OPENAI_API_KEY", "CODEX_API_KEY")

server = MCPServer(
    "ai-bridge",
    instructions=(
        "Other models on Jashan's free sign-ins. gemini_search: quick web answers. ask_gemini: long PDFs, many files "
        "or long research, returned as a summary. ask_gpt: a second opinion from ChatGPT (small free allowance)."
    ),
    log_level="WARNING",
)


def tool(annotations: ToolAnnotations) -> Callable:
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ToolError:
                raise
            except Exception as e:  # never let a tool fail with no explanation
                raise ToolError(f"{type(e).__name__}: {e}") from e

        server.tool(annotations=annotations, structured_output=False)(wrapper)
        return fn

    return decorator


def find_cli(name: str) -> str:
    """The CLI, even when launched without the login shell's PATH (nvm puts node tools out of the way)."""
    found = shutil.which(name) or next(
        iter(sorted(glob.glob(str(Path.home() / f".nvm/versions/node/*/bin/{name}")))), ""
    )
    if not found:
        raise ToolError(
            f"The `{name}` CLI isn't installed. Install it with npm, then sign in once by running `{name}`."
        )
    return found


def cli_env(binary: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in BILLING_VARS}
    # The CLIs are node scripts (`#!/usr/bin/env node`): node lives next to them under nvm.
    env["PATH"] = f"{Path(binary).parent}:{env.get('PATH', '/usr/bin:/bin')}"
    return env


def resolve_files(files: list[str]) -> list[Path]:
    paths = []
    for name in files:
        path = Path(name).expanduser()
        if not path.is_absolute():
            raise ToolError(f"Use a full path for '{name}' (e.g. ~/Documents/...).")
        if not path.exists():
            raise ToolError(f"No file at {path}.")
        paths.append(path.resolve())
    return paths


def _clip(text: str) -> str:
    text = text.strip()
    return text[:MAX_REPLY_CHARS] + ("\n[truncated]" if len(text) > MAX_REPLY_CHARS else "")


def run_gemini(prompt: str, files: list[Path], timeout: int) -> str:
    binary = find_cli("gemini")
    dirs = sorted({str(p if p.is_dir() else p.parent) for p in files})
    if files:
        prompt += "\n\nFiles to read (use your file tools on these paths):\n" + "\n".join(f"- {p}" for p in files)
    command = [binary, "--approval-mode", "plan", "--skip-trust", "-o", "json", "-p", prompt]
    if dirs:
        command += ["--include-directories", ",".join(dirs)]
    if GEMINI_MODEL:
        command += ["-m", GEMINI_MODEL]
    with tempfile.TemporaryDirectory(prefix="bridge-") as empty:
        try:
            done = subprocess.run(
                command, cwd=empty, env=cli_env(binary), capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL,
            )  # fmt: skip
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"Gemini took longer than {timeout} s and was stopped.") from e
    return parse_gemini(done.stdout, done.stderr, done.returncode)


def parse_gemini(stdout: str, stderr: str, code: int) -> str:
    if "set an Auth method" in stdout + stderr or "Please sign in" in stdout + stderr:
        raise ToolError(
            "Gemini has no key. Make a free one at aistudio.google.com/apikey and put GEMINI_API_KEY=... "
            "in ~/.gemini/.env (free sign-in for Gemini CLI ended in June 2026)."
        )
    start = stdout.find("{")
    if start >= 0:
        try:
            data = json.loads(stdout[start:])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            if data.get("error"):
                error = data["error"]
                message = error.get("message", error) if isinstance(error, dict) else error
                raise ToolError(f"Gemini error: {message}")
            if data.get("response"):
                return _clip(data["response"])
    if code != 0:
        raise ToolError(f"Gemini failed (exit {code}): {(stderr or stdout).strip()[-500:]}")
    return _clip(stdout)


def run_codex(prompt: str, files: list[Path], timeout: int) -> str:
    binary = find_cli("codex")
    if files:
        prompt += "\n\nFiles to read:\n" + "\n".join(f"- {p}" for p in files)
    with tempfile.TemporaryDirectory(prefix="bridge-") as empty:
        answer = Path(empty) / "answer.txt"
        command = [binary, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "-C", empty,
                   "-o", str(answer)]  # fmt: skip
        if CODEX_MODEL:
            command += ["-m", CODEX_MODEL]
        command.append(prompt)
        try:
            done = subprocess.run(
                command, env=cli_env(binary), capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL,
            )  # fmt: skip
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"ChatGPT (Codex) took longer than {timeout} s and was stopped.") from e
        text = answer.read_text() if answer.exists() else ""
    return parse_codex(text, done.stdout + done.stderr, done.returncode)


def parse_codex(answer: str, output: str, code: int) -> str:
    if answer.strip():
        return _clip(answer)
    if "not logged in" in output.lower() or ("login" in output.lower() and code != 0):
        raise ToolError("Codex isn't signed in. Run `codex` once in Terminal and choose Sign in with ChatGPT.")
    for line in output.splitlines():
        if "usage limit" in line.lower() or "rate limit" in line.lower():
            raise ToolError(f"ChatGPT's free allowance is used up for now: {line.strip()[:300]}")
        if line.startswith("ERROR"):
            raise ToolError(f"Codex error: {line.strip()[:500]}")
    raise ToolError(f"Codex returned nothing (exit {code}): {output.strip()[-500:]}")


@tool(READ)
def gemini_search(question: str) -> str:
    """Answer a question from the web with Google Search, through Gemini. Free — prefer it over paid search.

    Args:
        question: What to find out, e.g. "UW Fall 2026 reading week dates" or "is the ION running on Sundays".
    """
    return run_gemini(
        "Answer using Google Search. Be concise and factual; give the answer first, then at most three "
        f"sources as plain URLs.\n\nQuestion: {question}",
        [],
        SEARCH_TIMEOUT,
    )


@tool(READ)
def ask_gemini(task: str, files: list[str] | None = None) -> str:
    """Hand heavy reading to Gemini (free, 1M-token context): long or scanned PDFs, many files, long research.

    It reads everything itself and returns what the task asks for, so only the summary comes back.
    Nothing is edited. Files are sent to Google: pass only what the task needs.

    Args:
        task: What to produce, e.g. "Explain every concept in this lecture, with the worked examples,
            as study notes" or "Research X across the web and compare the options".
        files: Full paths of PDFs, images or text files to read, e.g. ["~/Documents/.../L3.pdf"].
    """
    return run_gemini(task, resolve_files(files or []), READ_TIMEOUT)


@tool(READ)
def ask_gpt(task: str, files: list[str] | None = None) -> str:
    """Ask ChatGPT (through Codex, read-only) for a second opinion or a hard problem another model handles well.

    The free plan's allowance is small: use it when a different model's view is worth it.

    Args:
        task: The question or problem, self-contained.
        files: Optional full paths it should read.
    """
    return run_codex(task, resolve_files(files or []), GPT_TIMEOUT)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
