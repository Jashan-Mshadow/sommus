"""`sommus model-nightly`: the whole night, stage by stage. A stage that fails is recorded and the rest still run;
the progress report is always written, and the summary is returned so a scheduler can send it."""

from __future__ import annotations

import os
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sommus import config
from sommus.brain.store import Store, Usage
from sommus.modeltrain import common, evaluate, export, report, schemas, synth, train

DEFAULTS: dict[str, Any] = {
    "enabled": True,  # the data stages (export, split, synthetic, report)
    "train": True,  # the fine-tune: still only runs when the hardware check passes
    "base_model": "mlx-community/Qwen2.5-1.5B-Instruct-4bit",
    "lora_rank": 8,
    "lora_scale": 20.0,
    "lora_dropout": 0.0,
    "num_layers": 16,
    "iters": 600,
    "batch_size": 4,
    "learning_rate": 1e-4,
    "max_seq_length": 512,
    "min_memory_gb": 16,
    "min_train_examples": 50,
    "timeout_minutes": 90,
    "synthetic_per_night": 40,
    "synthetic_max_usd": 0.05,
    "synthetic_model": "",  # "" = [budget] cheap_model (Haiku)
    "synthetic_tools_per_night": 10,
    "exclude_tools": [
        "run_shell",
        "type_text",
        "press_keys",
        "click",
        "screenshot",
        "ask_claude",
        "ask_gemini",
        "ask_gpt",
    ],
    "vault": "~/Documents/Jashans_Brain",
    "note": "Portfolio/Projects/Sommus Model Progress.md",
}


def settings(path: Path | None = None) -> dict:
    found = {**DEFAULTS, **config.section("model_training", path)}
    if not found["synthetic_model"]:
        found["synthetic_model"] = config.section("budget", path).get("cheap_model", "claude-haiku-4-5")
    return found


@dataclass
class Night:
    summary: str
    record: dict
    log: list[str] = field(default_factory=list)


def run(
    root: Path,
    cfg: dict,
    *,
    client: Any = None,
    now: datetime | None = None,
    runner: Callable[..., Any] | None = None,
    hardware: str | None | bool = False,
    model: evaluate.Predict | None = None,
    say: Callable[[str], None] = lambda line: None,
) -> Night:
    now = now or datetime.now()
    cfg = {**cfg, "synthetic_model": cfg.get("synthetic_model") or "claude-haiku-4-5"}
    data = root / "data"
    tdir = data / "training"
    files = {n: tdir / f"{n}.jsonl" for n in ("real_train", "real_test", "synthetic_train", "synthetic_test")}
    manifest_path, progress_path = tdir / "manifest.json", tdir / "progress.jsonl"
    manifest = {"last_turn_id": 0, "trained_examples": 0, "last_adapter": "", **_read_manifest(manifest_path)}
    log: list[str] = []
    errors: list[str] = []

    def note(line: str) -> None:
        log.append(line)
        say(line)

    night = report.night_number(progress_path)
    stamp = now.strftime("%Y%m%d")
    sets = {n: common.read_jsonl(p) for n, p in files.items()}
    known = {common.key(ex["request"]) for rows in sets.values() for ex in rows}
    cost = 0.0
    real_new = synth_new = 0
    synth_status, synth_reason = "skipped", "disabled"
    result_train = train.TrainResult("skipped", "disabled in config", "off in config")
    evaluation: dict | None = None

    if not cfg["enabled"]:
        note("model training is off ([model_training] enabled = false): nothing ran")
    else:
        # 1 + 2. export and split
        try:
            out = export.export_new(data / "sommus.db", int(manifest["last_turn_id"]), known, now)
            common.append_jsonl(files["real_train"], out.train)
            common.append_jsonl(files["real_test"], out.test)
            sets["real_train"] += out.train
            sets["real_test"] += out.test
            known |= {common.key(e["request"]) for e in out.train + out.test}
            manifest["last_turn_id"] = out.last_id
            real_new = len(out.train) + len(out.test)
            note(
                f"export: {real_new} new real examples ({len(out.train)} train, {len(out.test)} test); dropped "
                f"{out.dropped_secret} with secrets, {out.dropped_duplicate} duplicates, "
                f"{out.dropped_internal} internal"
            )
        except Exception as e:
            errors.append(f"export failed: {e}")
            note(errors[-1])

        # 3. synthetic expansion
        try:
            tools = schemas.load_tools(data / "nodes")
            counts = Counter(c["tool"] for rows in sets.values() for ex in rows for c in ex["calls"])
            res = synth.generate(
                tools, counts, known, client=client, model=cfg["synthetic_model"], want=int(cfg["synthetic_per_night"]),
                max_cost_usd=float(cfg["synthetic_max_usd"]), tools_per_night=int(cfg["synthetic_tools_per_night"]),
                exclude=set(cfg["exclude_tools"]), stamp=stamp,
            )  # fmt: skip
            synth_status, synth_reason = res.status, res.reason
            if res.status == "ok":
                for ex in res.examples:
                    bucket = "synthetic_test" if common.is_test(ex["request"]) else "synthetic_train"
                    sets[bucket].append(ex)
                    common.append_jsonl(files[bucket], [ex])
                synth_new = len(res.examples)
                cost += res.cost_usd
                _log_spend(data / "sommus.db", cfg["synthetic_model"], res)
                note(
                    f"synthetic: {synth_new} kept of {res.generated} generated ({res.invalid} invalid, "
                    f"{res.duplicate} duplicates), aimed at {', '.join(res.targets[:5])}; ${res.cost_usd:.4f}"
                    + (f"; {res.reason}" if res.reason else "")
                )
            else:
                note(f"synthetic: skipped: {res.reason}")
        except Exception as e:
            errors.append(f"synthetic failed: {e}")
            synth_status, synth_reason = "skipped", "failed"
            note(errors[-1])

        # 4. train
        try:
            train_set = sets["real_train"] + sets["synthetic_train"]
            kwargs = {"runner": runner} if runner else {}
            result_train = train.run(
                cfg, train_set, tdir, stamp, int(manifest["trained_examples"]), hardware=hardware, **kwargs
            )
            if result_train.status == "trained":
                manifest["trained_examples"] = result_train.examples
                manifest["last_adapter"] = result_train.adapter
            note(f"train: {result_train.status}: {result_train.reason}")
        except Exception as e:
            result_train = train.TrainResult("failed", str(e), "failed")
            errors.append(f"train failed: {e}")
            note(errors[-1])

        # 5. evaluate
        try:
            candidate = model
            if candidate is None and manifest["last_adapter"] and train.hardware_problem(cfg["min_memory_gb"]) is None:
                candidate = evaluate.load_mlx_model(cfg["base_model"], manifest["last_adapter"])
            evaluation = evaluate.run_all(sets["real_test"], sets["synthetic_test"], candidate)
            base = evaluation["baseline"]["real"]
            what = "candidate model scored" if candidate else "no trained model, baseline (always escalate) only"
            seen = (
                f"real baseline score {base['score']:.2f} on {base['n']}"
                if base.get("n")
                else "no real test examples yet"
            )
            note(f"eval: {what}; {seen}")
        except Exception as e:
            errors.append(f"eval failed: {e}")
            note(errors[-1])

    all_examples = [ex for rows in sets.values() for ex in rows]
    covered = {c["tool"] for ex in all_examples for c in ex["calls"]}
    total_tools = set(schemas.load_tools(data / "nodes")) | covered
    record = {
        "at": now.isoformat(timespec="seconds"),
        "night": night,
        "real_total": len(sets["real_train"]) + len(sets["real_test"]),
        "real_new": real_new,
        "synthetic_total": len(sets["synthetic_train"]) + len(sets["synthetic_test"]),
        "synthetic_new": synth_new,
        "synthetic_status": synth_status,
        "synthetic_reason": synth_reason,
        "test_total": len(sets["real_test"]) + len(sets["synthetic_test"]),
        "test_real": len(sets["real_test"]),
        "test_synthetic": len(sets["synthetic_test"]),
        "tools_covered": len(covered),
        "tools_total": len(total_tools),
        "train": {
            "status": result_train.status,
            "reason": result_train.reason,
            "short": result_train.short,
            "examples": result_train.examples,
        },
        "eval": evaluation,
        "cost_usd": round(cost, 6),
        "errors": errors,
    }
    text = report.summary(record)
    manifest["last_run"] = record["at"]
    manifest["nights"] = night
    try:
        common.write_json(manifest_path, manifest)
        report.append_progress(progress_path, record)
        vault = Path(os.path.expanduser(cfg["vault"]))
        note(report.append_note(vault / cfg["note"], vault, record, text, log))
    except Exception as e:  # the report must not be the thing that fails loudly
        log.append(f"report failed: {e}")
    return Night(text, record, log)


def _read_manifest(path: Path) -> dict:
    import json

    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _log_spend(db_path: Path, model: str, res: synth.SynthResult) -> None:
    """The synthetic calls are real API spend: put them in the main log so the monthly budget counts them."""
    if not res.cost_usd or not db_path.exists():
        return
    store = Store(db_path)
    turn = store.start_turn("[model-nightly] synthetic requests")
    store.finish_turn(turn, "", "ok", model, Usage(input_tokens=res.input_tokens, output_tokens=res.output_tokens))


def _stderr(line: str) -> None:
    print(f"  {line}", file=sys.stderr, flush=True)


def run_from_config(say: Callable[[str], None] = _stderr) -> Night:
    """Stage logs go to stderr; the one-line summary is the command's stdout, for a scheduler to send."""
    from dotenv import load_dotenv

    load_dotenv(config.ROOT / ".env")
    return run(config.ROOT, settings(), say=say)


def main() -> None:
    print(run_from_config().summary)
