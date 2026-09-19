"""sommus stats: aggregates only, never request text."""

import json

from sommus import stats
from sommus.brain.store import Store


def test_measurements_page_has_numbers_but_no_request_text(tmp_path):
    store = Store(tmp_path / "s.db")
    store._db.execute(
        "INSERT INTO turns (started_at, user_text, status, model, cost_usd, input_tokens, cache_read_tokens,"
        " cache_write_tokens) VALUES ('2026-09-19T10:00:00', 'text didi a secret plan', 'ok', 'claude-sonnet-5',"
        " 0.007, 100, 900, 0)"
    )
    store._db.execute(
        "INSERT INTO turns (started_at, user_text, status, model, cost_usd) VALUES"
        " ('2026-09-19T10:01:00', 'mute', 'ok', 'fastpath', 0)"
    )
    store._db.commit()
    evals = tmp_path / "evals"
    evals.mkdir()
    (evals / "a.json").write_text(
        json.dumps({"ran_at": "2026-09-19T15:45:00", "model": "claude-haiku-4-5", "effort": "low", "passed": 41,
                    "total": 46, "cost_usd": 0.12, "cases": [{"seconds": 2.0}]})
    )  # fmt: skip
    page = stats.render(tmp_path / "s.db", evals)
    assert "| Commands handled | 2 |" in page and "| Answered with no model call (fast path) | 1 |" in page
    assert "90%" in page and "0.70¢" in page
    assert "| Haiku 4.5 | — | 41/46 | 0.26¢ | 2.0 s |" in page
    assert "didi" not in page and "secret" not in page and "mute" not in page
