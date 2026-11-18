"""Stage 6: the night's progress: one JSON line, a plain-language summary for Telegram, a note in the vault."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sommus.modeltrain import common

NOTE_HEADER = (
    "# Sommus Model Progress\n\n"
    "One section a night from `sommus model-nightly`: how much data the small model has, whether it trained, "
    "and how it scores against Claude. Written by the job, not by hand.\n"
)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def eval_text(evaluation: dict | None) -> str:
    if not evaluation:
        return "eval: skipped"
    cand = evaluation.get("candidate")
    if not cand:
        return "eval: baseline only"
    real, base = cand["real"], evaluation["baseline"]["real"]
    if not real.get("n"):
        return "eval: no real test examples yet"
    return (
        f"eval: tools {_pct(real['tool_accuracy'])}, args {_pct(real['arg_accuracy'])}, "
        f"score {real['score']:.2f} vs always-escalate {base['score']:.2f}"
    )


def summary(r: dict) -> str:
    syn = f"{r['synthetic_total']} synthetic (+{r['synthetic_new']}"
    syn += f", skipped: {r['synthetic_reason']})" if r["synthetic_status"] == "skipped" else ")"
    train = r["train"]
    if train["status"] == "skipped":
        trained = f"training: skipped ({train['short']})"
    elif train["status"] == "failed":
        trained = f"training: FAILED ({train['short']})"
    else:
        trained = f"training: done on {train['examples']} examples"
    parts = [
        f"Night {r['night']}: {r['real_total']} real examples (+{r['real_new']})",
        syn,
        f"test set {r['test_total']}",
        f"tools covered {r['tools_covered']}/{r['tools_total']}",
        trained,
        eval_text(r.get("eval")),
        f"cost ${r['cost_usd']:.2f}",
    ]
    if r.get("errors"):
        parts.append("problems: " + "; ".join(r["errors"]))
    return ", ".join(parts)


def append_progress(path: Path, record: dict) -> None:
    common.append_jsonl(path, [record])


def night_number(path: Path) -> int:
    return len(common.read_jsonl(path)) + 1


def append_note(note: Path, vault: Path, record: dict, text: str, log: list[str]) -> str:
    """Add a dated section; create the note with a header if missing. Returns what happened, in a few words."""
    if not vault.is_dir():
        return f"note skipped: vault not found at {vault}"
    note.parent.mkdir(parents=True, exist_ok=True)
    if not note.exists():
        note.write_text(NOTE_HEADER)
    day = datetime.fromisoformat(record["at"]).strftime("%Y-%m-%d")
    body = "\n".join(f"- {line}" for line in log)
    with note.open("a") as f:
        f.write(f"\n## {day} (night {record['night']})\n\n{text}\n\n{body}\n")
    return f"note updated: {note}"
