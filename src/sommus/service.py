"""Run Sommus in the background, detached from the terminal that started it.

Deliberately not a LaunchAgent: macOS attaches Accessibility and Automation
permissions to the *responsible* app, which for a launchd job is the bare Python
binary — every grant would have to be redone, and prompts would appear with nobody
there to click them. Started from Terminal, the process inherits Terminal's grants
and keeps them for its whole life, including after the window closes.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from sommus.config import Config


def pid_file(cfg: Config) -> Path:
    return cfg.data_dir / "telegram.pid"


def log_file(cfg: Config) -> Path:
    return cfg.data_dir / "telegram.log"


def running_pid(cfg: Config) -> int | None:
    path = pid_file(cfg)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)  # signal 0 just asks "is it alive?"
    except (ValueError, ProcessLookupError, PermissionError):
        path.unlink(missing_ok=True)
        return None
    return pid


def start(cfg: Config) -> tuple[int, Path]:
    existing = running_pid(cfg)
    if existing:
        raise RuntimeError(f"Already running (pid {existing}).")
    log = log_file(cfg)
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = log.open("a")
    handle.write(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    handle.flush()
    process = subprocess.Popen(
        [sys.executable, "-m", "sommus.interfaces.cli", "telegram"],
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # survives the terminal window closing
        env=os.environ.copy(),
    )
    pid_file(cfg).write_text(str(process.pid))
    time.sleep(2)  # long enough for a bad token or missing key to show up in the log
    if process.poll() is not None:
        pid_file(cfg).unlink(missing_ok=True)
        raise RuntimeError(f"It exited immediately. Last lines:\n{tail(cfg, 10)}")
    return process.pid, log


def stop(cfg: Config) -> int | None:
    pid = running_pid(cfg)
    if pid is None:
        return None
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
    pid_file(cfg).unlink(missing_ok=True)
    return pid


def tail(cfg: Config, lines: int = 20) -> str:
    log = log_file(cfg)
    if not log.exists():
        return "(no log yet)"
    return "\n".join(log.read_text(errors="replace").splitlines()[-lines:])
