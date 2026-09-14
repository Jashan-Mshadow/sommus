"""Background service bookkeeping: pid file, liveness, logs."""

import subprocess
import sys
from dataclasses import replace

from fakes import config as base_config

from sommus import service


def cfg(tmp_path):
    return replace(base_config(tmp_path), data_dir=tmp_path)


def test_nothing_is_running_before_it_starts(tmp_path):
    assert service.running_pid(cfg(tmp_path)) is None
    assert service.tail(cfg(tmp_path)) == "(no log yet)"


def test_a_stale_pid_file_is_cleaned_up(tmp_path):
    settings = cfg(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    service.pid_file(settings).write_text(str(dead.pid))

    assert service.running_pid(settings) is None
    assert not service.pid_file(settings).exists()


def test_a_live_process_is_reported_and_can_be_stopped(tmp_path):
    settings = cfg(tmp_path)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    service.pid_file(settings).write_text(str(sleeper.pid))

    assert service.running_pid(settings) == sleeper.pid
    assert service.stop(settings) == sleeper.pid
    assert not service.pid_file(settings).exists()
    assert sleeper.wait(timeout=5) is not None  # it really took the signal


def test_stopping_when_nothing_runs_is_harmless(tmp_path):
    assert service.stop(cfg(tmp_path)) is None


def test_tail_returns_the_last_lines(tmp_path):
    settings = cfg(tmp_path)
    service.log_file(settings).parent.mkdir(parents=True, exist_ok=True)
    service.log_file(settings).write_text("\n".join(f"line {i}" for i in range(50)))
    assert service.tail(settings, 3).splitlines() == ["line 47", "line 48", "line 49"]
