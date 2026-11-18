"""Stages 1 and 2: export new real interactions from the SQLite log, drop secrets, split train/test.

Incremental: the manifest remembers the last turn id exported, so a night only reads what is new. A turn still
being written (no status, started in the last hour) holds the pointer back, so it is picked up once it finishes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sommus import training
from sommus.modeltrain import common
from sommus.modeltrain.secrets import looks_secret, redact

OUTPUT_CLIP = 300
INTERNAL_PREFIXES = ("[diary]", "[model-nightly]", "[PIN entered]")


@dataclass
class Exported:
    train: list[dict] = field(default_factory=list)
    test: list[dict] = field(default_factory=list)
    dropped_secret: int = 0
    dropped_duplicate: int = 0
    dropped_internal: int = 0
    last_id: int = 0


def to_example(record: dict) -> dict:
    return {
        "id": f"real-{record['id']}",
        "source": "real",
        "request": " ".join(record["request"].split()),
        "calls": [
            {"tool": t["tool"], "args": t["args"] if isinstance(t["args"], dict) else {}} for t in record["tools"]
        ],
        "error": any(t["error"] for t in record["tools"]) or record["status"] not in ("ok", None),
        "route": record["route"],
        "at": record["at"],
        "output": redact(" | ".join(t["output"] for t in record["tools"]))[:OUTPUT_CLIP],
    }


def export_new(db_path: Path, last_id: int, known_keys: set[str], now: datetime | None = None) -> Exported:
    """New real examples since `last_id`, split by request hash. `known_keys` are requests already in any set."""
    out = Exported(last_id=last_id)
    if not db_path.exists():
        return out
    now = now or datetime.now()
    recent = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as db:
        stuck = db.execute("SELECT MIN(id) FROM turns WHERE status IS NULL AND started_at >= ?", (recent,)).fetchone()[
            0
        ]
        found = training.records(db, after_id=last_id, before_id=stuck)
    seen = set(known_keys)
    for record in found:
        out.last_id = max(out.last_id, record["id"])
        if record["request"].startswith(INTERNAL_PREFIXES) or record["status"] is None:
            out.dropped_internal += 1
            continue
        example = to_example(record)
        if looks_secret(example["request"]) or looks_secret([c["args"] for c in example["calls"]]):
            out.dropped_secret += 1
            continue
        k = common.key(example["request"])
        if not k or k in seen:
            out.dropped_duplicate += 1
            continue
        seen.add(k)
        (out.test if common.is_test(example["request"]) else out.train).append(example)
    return out
