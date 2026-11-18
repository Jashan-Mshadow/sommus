"""`sommus export-training`: one clean JSONL record per turn, with its tools, and no PIN turns."""

import json

from sommus import training
from sommus.brain.store import Store, Usage


def test_the_export_has_one_record_per_turn_with_its_tools_and_leaves_pin_turns_out(tmp_path):
    store = Store(tmp_path / "t.db")
    turn = store.start_turn("took my creatine")
    store.finish_turn(turn, "Logged creatine.", "ok", "fastpath", Usage())
    store.finish_turn(store.start_turn("[PIN entered]"), "Unlocked.", "ok", "pin", Usage())
    store.finish_turn(store.start_turn("what's the weather"), "Cold.", "ok", "claude-sonnet-5", Usage())

    out = tmp_path / "out" / "requests.jsonl"
    assert training.write(tmp_path / "t.db", out) == 2
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["request"] for r in rows] == ["took my creatine", "what's the weather"]
    assert [r["route"] for r in rows] == ["fastpath", "model"]
    assert rows[0]["reply"] == "Logged creatine."
