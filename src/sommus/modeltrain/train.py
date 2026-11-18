"""Stage 4: the fine-tune. Pluggable and honest: it runs an MLX LoRA fine-tune only where that can work, and
otherwise says exactly why it did not. Nothing here pretends a model was trained.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sommus.modeltrain import common

SYSTEM_PROMPT = (
    "You are Sommus's small model. Answer with JSON only: "
    '{"calls": [{"tool": ..., "args": {...}}]} for a request you can handle, or {"escalate": true} otherwise.'
)


@dataclass
class TrainResult:
    status: str  # trained | skipped | failed
    reason: str = ""
    short: str = ""  # the few words that go in the Telegram line
    adapter: str = ""
    examples: int = 0


def memory_gb() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except (ValueError, OSError, AttributeError):
        return 0.0


def hardware_problem(min_memory_gb: float, *, system=None, machine=None, ram_gb=None, has_mlx=None) -> str | None:
    """None when this machine can fine-tune, else the exact reason it cannot. Arguments exist for tests."""
    system = system or platform.system()
    machine = machine or platform.machine()
    ram = memory_gb() if ram_gb is None else ram_gb
    has_mlx = (importlib.util.find_spec("mlx_lm") is not None) if has_mlx is None else has_mlx
    here = f"this machine: {system} {machine}, {ram:.0f} GB RAM"
    if system != "Darwin" or machine != "arm64":
        return f"needs Apple silicon ({here})"
    if ram < min_memory_gb:
        return f"needs {min_memory_gb:.0f} GB of memory ({here})"
    if not has_mlx:
        return "mlx-lm is not installed (uv pip install mlx-lm)"
    return None


def short_reason(reason: str, min_memory_gb: float) -> str:
    if reason.startswith("needs"):
        return f"needs {min_memory_gb:.0f} GB Apple silicon"
    return "mlx-lm missing" if reason.startswith("mlx-lm") else reason


def chat_record(example: dict) -> dict:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": example["request"]},
            {"role": "assistant", "content": json.dumps(common.target(example), ensure_ascii=False, sort_keys=True)},
        ]
    }


def build_dataset(train: list[dict], out_dir: Path) -> int:
    """Write mlx_lm's train.jsonl and valid.jsonl (10% by request hash, never from the test sets)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fit, valid = [], []
    for ex in train:
        (valid if common.is_test(ex["request"] + "#valid", 10) else fit).append(chat_record(ex))
    if not valid and fit:
        valid.append(fit[-1])
    for name, rows in (("train.jsonl", fit), ("valid.jsonl", valid)):
        (out_dir / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return len(fit)


def lora_command(settings: dict, data_dir: Path, adapter_dir: Path, config_file: Path) -> list[str]:
    return [
        sys.executable, "-m", "mlx_lm.lora",
        "--model", settings["base_model"],
        "--train",
        "--data", str(data_dir),
        "--adapter-path", str(adapter_dir),
        "--iters", str(settings["iters"]),
        "--batch-size", str(settings["batch_size"]),
        "--num-layers", str(settings["num_layers"]),
        "--learning-rate", str(settings["learning_rate"]),
        "--max-seq-length", str(settings["max_seq_length"]),
        "-c", str(config_file),
    ]  # fmt: skip


def write_lora_config(path: Path, settings: dict) -> None:
    path.write_text(
        "lora_parameters:\n"
        f"  rank: {int(settings['lora_rank'])}\n"
        f"  scale: {float(settings['lora_scale'])}\n"
        f"  dropout: {float(settings['lora_dropout'])}\n"
    )


def run(
    settings: dict,
    train_examples: list[dict],
    work_dir: Path,
    stamp: str,
    already_trained_on: int,
    *,
    hardware: str | None | bool = False,
    runner: Callable[..., Any] = subprocess.run,
) -> TrainResult:
    """`hardware=False` means check the real machine; tests pass a reason string or None."""
    if not settings.get("train", True):
        return TrainResult("skipped", "train = false in [model_training]", "off in config")
    problem = hardware_problem(float(settings["min_memory_gb"])) if hardware is False else hardware
    if problem:
        return TrainResult("skipped", problem, short_reason(problem, float(settings["min_memory_gb"])))
    n = len(train_examples)
    if n < int(settings["min_train_examples"]):
        why = f"only {n} train examples, needs {settings['min_train_examples']}"
        return TrainResult("skipped", why, f"{n} examples is too few")
    if n <= already_trained_on:
        return TrainResult("skipped", f"no new examples since the last run ({already_trained_on})", "no new data")
    data_dir, adapter_dir = work_dir / "mlx", work_dir / "adapters" / stamp
    fit = build_dataset(train_examples, data_dir)
    config_file = work_dir / "lora.yaml"
    work_dir.mkdir(parents=True, exist_ok=True)
    write_lora_config(config_file, settings)
    cmd = lora_command(settings, data_dir, adapter_dir, config_file)
    try:
        done = runner(cmd, capture_output=True, text=True, timeout=float(settings["timeout_minutes"]) * 60)
    except (subprocess.TimeoutExpired, OSError) as e:
        return TrainResult("failed", f"{type(e).__name__}: {e}", "failed (timeout or launch error)")
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or ["no output"]
        return TrainResult("failed", f"mlx_lm.lora exited {done.returncode}: {tail[0][:200]}", "failed")
    return TrainResult("trained", f"{fit} examples, adapter at {adapter_dir}", "trained", str(adapter_dir), n)
