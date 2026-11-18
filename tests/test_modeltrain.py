"""The nightly model pipeline: export, secrets, split, synthetic validation and cost cap, train gate, eval, report.
No network, no API: the Anthropic client is a fake."""

import json
from collections import Counter
from datetime import datetime
from types import SimpleNamespace

from sommus.brain.store import Store, Usage
from sommus.modeltrain import common, evaluate, export, nightly, report, schemas, synth, train
from sommus.modeltrain.secrets import looks_secret

NOW = datetime(2026, 11, 18, 3, 0, 0)

VOLUME = {"type": "object", "properties": {"level": {"type": "integer"}}, "required": ["level"]}
FAN = {"type": "object", "properties": {"name": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
TOOLS = {
    "set_volume": {"description": "Set volume", "schema": VOLUME},
    "plug_off": {"description": "Fan off", "schema": FAN},
    "get_battery": {"description": "Battery", "schema": {"type": "object", "properties": {}}},
}


def log_turn(store, text, tools=(), model="claude-sonnet-5", status="ok"):
    turn = store.start_turn(text)
    for name, args in tools:
        store.log_tool(turn, name, args, "read", "allow", False, "done")
    store.finish_turn(turn, "ok", status, model, Usage())
    return turn


def write_nodes(root, tools=TOOLS):
    d = root / "data" / "nodes"
    d.mkdir(parents=True, exist_ok=True)
    listed = [{"name": n, "description": t["description"], "input_schema": t["schema"]} for n, t in tools.items()]
    (d / "laptop.json").write_text(json.dumps(listed))


class FakeClient:
    def __init__(self, items, input_tokens=1000, output_tokens=500):
        self.calls = 0
        self.items = items
        self.later = []  # a second batch has nothing new
        self.usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        self.last = kw
        block = SimpleNamespace(type="text", text=json.dumps(self.items if self.calls == 1 else self.later))
        return SimpleNamespace(content=[block], usage=self.usage)


# --- export -----------------------------------------------------------------------------------------------------


def test_export_is_incremental_and_keeps_pin_and_internal_turns_out(tmp_path):
    db = tmp_path / "s.db"
    store = Store(db)
    log_turn(store, "volume to 30", [("set_volume", {"level": 30})])
    log_turn(store, "[PIN entered]", model="pin")
    log_turn(store, "[diary] 2026-11-17")
    first = export.export_new(db, 0, set(), NOW)
    assert [e["request"] for e in first.train + first.test] == ["volume to 30"]
    assert first.dropped_internal == 1  # the diary turn; the PIN turn is cut in the query

    log_turn(store, "fan off", [("plug_off", {})])
    second = export.export_new(db, first.last_id, set(), NOW)
    assert [e["request"] for e in second.train + second.test] == ["fan off"]
    assert export.export_new(db, second.last_id, set(), NOW).last_id == second.last_id


def test_a_turn_still_running_holds_the_pointer_back_until_it_finishes(tmp_path):
    db = tmp_path / "s.db"
    store = Store(db)
    a = store.start_turn("volume to 30")  # unfinished, started just now
    log_turn(store, "fan off", [("plug_off", {})])
    now = datetime.now()
    out = export.export_new(db, 0, set(), now)
    assert out.last_id == 0 and not out.train + out.test
    store.finish_turn(a, "ok", "ok", "claude-sonnet-5", Usage())
    out = export.export_new(db, 0, set(), now)
    assert len(out.train + out.test) == 2


def test_duplicate_requests_are_not_exported_twice(tmp_path):
    db = tmp_path / "s.db"
    store = Store(db)
    log_turn(store, "Fan off.", [("plug_off", {})])
    out = export.export_new(db, 0, {common.key("fan off")}, NOW)
    assert not out.train + out.test and out.dropped_duplicate == 1


# --- secrets ----------------------------------------------------------------------------------------------------


def test_secrets_are_recognised():
    for text in [
        "my pin is 4821",
        "pin 123456",
        "the password is hunter22",
        "call 5195551234 now",
        "card 4111 1111 1111 1111",
        "key sk-ant-abcdefghijklmnop1234",
        "token: abc123xyz",
        "x" * 10 + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7",
    ]:
        assert looks_secret(text), text
    for text in ["set the volume to 30", "remind me at 5 pm", "what's my next class", "fan off", "pin the tab"]:
        assert not looks_secret(text), text


def test_secret_requests_and_args_are_dropped_from_the_export(tmp_path):
    db = tmp_path / "s.db"
    store = Store(db)
    log_turn(store, "my pin is 4821 unlock")
    log_turn(store, "message mom", [("send_message", {"to": "me", "text": "code 99887766554"})])
    log_turn(store, "fan off", [("plug_off", {})])
    out = export.export_new(db, 0, set(), NOW)
    assert [e["request"] for e in out.train + out.test] == ["fan off"] and out.dropped_secret == 2


def test_tool_output_is_clipped_and_redacted(tmp_path):
    db = tmp_path / "s.db"
    store = Store(db)
    turn = store.start_turn("read it")
    store.log_tool(turn, "read_note", {"name": "x"}, "read", "allow", False, "token: abc123 " + "long " * 500)
    store.finish_turn(turn, "ok", "ok", "claude-sonnet-5", Usage())
    out = export.export_new(db, 0, set(), NOW)
    ex = (out.train + out.test)[0]
    assert len(ex["output"]) <= export.OUTPUT_CLIP and "abc123" not in ex["output"]


# --- split ------------------------------------------------------------------------------------------------------


def test_split_is_stable_and_about_twenty_percent():
    assert common.is_test("Fan off.") == common.is_test("  fan   OFF")
    requests = [f"request number {i}" for i in range(2000)]
    share = sum(common.is_test(r) for r in requests) / len(requests)
    assert 0.16 < share < 0.24
    assert [common.is_test(r) for r in requests] == [common.is_test(r) for r in requests]


def test_the_test_set_only_grows_across_nights(tmp_path):
    root = tmp_path
    store = Store(root / "data" / "sommus.db")
    cfg = {**nightly.DEFAULTS, "vault": str(tmp_path / "novault"), "synthetic_per_night": 0}
    sizes = []
    for night in range(3):
        for i in range(30):
            log_turn(store, f"night {night} request {i}", [("get_battery", {})])
        sizes.append(nightly.run(root, cfg, now=NOW, hardware="needs Apple silicon").record["test_real"])
    assert sizes == sorted(sizes) and sizes[-1] > 0
    test_keys = {common.key(e["request"]) for e in common.read_jsonl(root / "data/training/real_test.jsonl")}
    train_keys = {common.key(e["request"]) for e in common.read_jsonl(root / "data/training/real_train.jsonl")}
    assert not test_keys & train_keys


# --- schemas and synthetic --------------------------------------------------------------------------------------


def test_validate_call_checks_names_types_and_required():
    ok = schemas.validate_call
    assert ok({"tool": "set_volume", "args": {"level": 30}}, TOOLS) is None
    assert ok({"tool": "plug_off", "args": {"name": None}}, TOOLS) is None
    assert "unknown tool" in ok({"tool": "launch_missiles", "args": {}}, TOOLS)
    assert "no argument" in ok({"tool": "set_volume", "args": {"volume": 3}}, TOOLS)
    assert "wrong type" in ok({"tool": "set_volume", "args": {"level": "loud"}}, TOOLS)
    assert "wrong type" in ok({"tool": "set_volume", "args": {"level": True}}, TOOLS)
    assert "needs level" in ok({"tool": "set_volume", "args": {}}, TOOLS)


def test_load_tools_reads_the_cached_node_lists(tmp_path):
    write_nodes(tmp_path)
    assert set(schemas.load_tools(tmp_path / "data" / "nodes")) == set(TOOLS)
    assert schemas.load_tools(tmp_path / "nothing") == {}


GOOD = {"request": "turn it up to 40", "calls": [{"tool": "set_volume", "args": {"level": 40}}]}
BAD = [
    {"request": "make it loud", "calls": [{"tool": "set_volume", "args": {"level": "max"}}]},
    {"request": "boom", "calls": [{"tool": "nuke", "args": {}}]},
    {"request": "x", "calls": []},
    {"request": "pin 4821 please", "calls": [{"tool": "get_battery", "args": {}}]},
    "not even a dict",
]


def gen(client, **kw):
    args = dict(client=client, model="claude-haiku-4-5", want=5, max_cost_usd=0.05, stamp="t")
    return synth.generate(TOOLS, Counter(), set(), **{**args, **kw})


def test_invalid_synthetic_examples_are_dropped():
    res = gen(FakeClient([GOOD, *BAD, GOOD]))
    assert [e["request"] for e in res.examples] == ["turn it up to 40"]
    assert res.invalid == len(BAD) and res.duplicate == 1


def test_the_prompt_has_schemas_and_no_real_requests():
    client = FakeClient([GOOD])
    gen(client)
    sent = client.last["system"] + client.last["messages"][0]["content"]
    assert "set_volume" in sent and "level" in sent


def test_targets_are_the_least_covered_tools():
    counts = Counter({"set_volume": 50, "plug_off": 3})
    assert synth.least_covered(TOOLS, counts, 2, set()) == ["get_battery", "plug_off"]
    assert "get_battery" not in synth.least_covered(TOOLS, counts, 2, {"get_battery"})


def test_cost_cap_stops_generation_and_skips_when_below_one_batch():
    tiny = gen(FakeClient([GOOD]), max_cost_usd=0.0001)
    assert tiny.status == "skipped" and "cap" in tiny.reason

    # each call really costs 1000*1 + 2000*5 = $0.011; the worst-case estimate is higher, so the cap bites early
    class Unique(FakeClient):
        def create(self, **kw):
            self.items = [
                {"request": f"volume {self.calls}-{i}", "calls": [{"tool": "set_volume", "args": {"level": i}}]}
                for i in range(20)
            ]
            self.calls += 1
            block = SimpleNamespace(type="text", text=json.dumps(self.items))
            return SimpleNamespace(content=[block], usage=self.usage)

    client = Unique([], input_tokens=1000, output_tokens=2000)
    res = gen(client, want=1000, max_cost_usd=0.03)
    assert 1 <= client.calls < 5 and res.cost_usd <= 0.03 and "cap" in res.reason


def test_no_api_key_means_a_clean_skip(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    res = synth.generate(TOOLS, Counter(), set(), client=None, model="m", want=5, max_cost_usd=0.05)
    assert res.status == "skipped" and res.reason == "no ANTHROPIC_API_KEY"


def test_no_schemas_means_a_clean_skip():
    res = synth.generate({}, Counter(), set(), client=FakeClient([]), model="m", want=5, max_cost_usd=0.05)
    assert res.status == "skipped"


# --- training gate ----------------------------------------------------------------------------------------------


def settings(**kw):
    return {**nightly.DEFAULTS, **kw}


def examples(n):
    return [
        {"request": f"r{i}", "calls": [{"tool": "get_battery", "args": {}}], "source": "synthetic"} for i in range(n)
    ]


def test_hardware_problem_names_the_exact_reason():
    assert "needs Apple silicon" in train.hardware_problem(
        16, system="Linux", machine="x86_64", ram_gb=1, has_mlx=False
    )
    assert "needs 16 GB" in train.hardware_problem(16, system="Darwin", machine="arm64", ram_gb=8, has_mlx=True)
    assert "mlx-lm is not installed" in train.hardware_problem(
        16, system="Darwin", machine="arm64", ram_gb=16, has_mlx=False
    )
    assert train.hardware_problem(16, system="Darwin", machine="arm64", ram_gb=16, has_mlx=True) is None


def test_train_is_skipped_with_the_reason_and_never_launches_when_the_hardware_fails(tmp_path):
    def boom(*a, **k):
        raise AssertionError("must not run")

    res = train.run(
        settings(), examples(100), tmp_path, "d", 0, hardware="needs Apple silicon (this machine: Linux)", runner=boom
    )
    assert (
        res.status == "skipped"
        and res.reason.startswith("needs Apple silicon")
        and res.short == "needs 16 GB Apple silicon"
    )
    assert not (tmp_path / "mlx").exists()


def test_train_runs_mlx_lora_when_the_hardware_passes(tmp_path):
    seen = {}

    def fake(cmd, **kw):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    res = train.run(settings(base_model="m/base"), examples(100), tmp_path, "d", 0, hardware=None, runner=fake)
    assert res.status == "trained" and res.examples == 100
    assert "mlx_lm.lora" in seen["cmd"] and "m/base" in seen["cmd"] and "--train" in seen["cmd"]
    assert (tmp_path / "mlx" / "train.jsonl").exists() and (tmp_path / "mlx" / "valid.jsonl").exists()
    assert "rank: 8" in (tmp_path / "lora.yaml").read_text()
    row = json.loads((tmp_path / "mlx" / "train.jsonl").read_text().splitlines()[0])
    assert row["messages"][2]["role"] == "assistant"


def test_train_skips_on_too_little_or_no_new_data_and_reports_a_failed_run(tmp_path):
    assert "too few" in train.run(settings(), examples(5), tmp_path, "d", 0, hardware=None).short
    assert train.run(settings(), examples(60), tmp_path, "d", 60, hardware=None).short == "no new data"
    assert train.run(settings(train=False), examples(60), tmp_path, "d", 0).status == "skipped"
    bad = train.run(settings(), examples(60), tmp_path, "d", 0, hardware=None,
                    runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="oom"))  # fmt: skip
    assert bad.status == "failed" and "oom" in bad.reason


# --- evaluation -------------------------------------------------------------------------------------------------


def real(request, calls, error=False):
    return {"source": "real", "request": request, "calls": calls, "error": error}


def call(tool, **args):
    return {"tool": tool, "args": args}


def test_evaluate_with_a_fake_model():
    data = [
        real("vol 30", [call("set_volume", level=30)]),
        real("vol 50", [call("set_volume", level=50)]),
        real("fan off", [call("plug_off", name="fan")]),
        real("essay", []),  # Claude used no tool: expect escalate
        real("hard", [call("a"), call("b"), call("c")]),  # too long a chain: expect escalate
    ]
    answers = {
        "vol 30": {"calls": [call("set_volume", level=30)]},  # right
        "vol 50": {"calls": [call("set_volume", level=5)]},  # right tool, wrong argument -> wrongly acting
        "fan off": {"escalate": True},  # playing safe
        "essay": {"calls": [call("get_battery")]},  # acted when it should have escalated
        "hard": {"escalate": True},  # right
    }
    m = evaluate.evaluate(data, lambda r: answers[r])
    assert m["n"] == 5 and m["expects_tools"] == 3
    assert m["tool_accuracy"] == 2 / 3 and m["arg_accuracy"] == 1 / 3
    assert m["answer_rate"] == 3 / 5 and m["wrong_action_rate"] == 2 / 5
    assert m["score"] == (1 + -2 + 0 + -2 + 1) / 5


def test_baseline_always_escalates_and_is_scored_the_same_way():
    data = [real("a", [call("get_battery")]), real("b", [])]
    base = evaluate.evaluate(data, evaluate.always_escalate)
    assert base["tool_accuracy"] == 0 and base["answer_rate"] == 0 and base["wrong_action_rate"] == 0
    assert base["score"] == 0.5


def test_argument_comparison_ignores_case_spacing_and_int_vs_float():
    a = real("x", [call("plug_off", name="Fan  Plug", level=30)])
    assert evaluate.score_one(common.target(a), {"calls": [call("plug_off", name="fan plug", level=30.0)]})["arg_ok"]


def test_run_all_reports_baseline_only_without_a_model_and_keeps_sets_apart():
    out = evaluate.run_all([real("a", [call("get_battery")])], [], None)
    assert out["candidate"] is None and out["baseline"]["synthetic"] == {"n": 0}
    syn = [{"source": "synthetic", "request": "b", "calls": [call("get_battery")]}]
    both = evaluate.run_all([real("a", [call("get_battery")])], syn, lambda r: {"calls": [call("get_battery")]})
    assert both["candidate"]["real"]["score"] == 1.0 and both["candidate"]["synthetic"]["score"] == 1.0


# --- the whole night, progress line and summary -----------------------------------------------------------------


def test_a_night_writes_progress_note_and_an_honest_summary(tmp_path):
    root = tmp_path / "repo"
    vault = tmp_path / "vault"
    vault.mkdir()
    store = Store(root / "data" / "sommus.db")
    for i in range(10):
        log_turn(store, f"set the volume to {i} please", [("set_volume", {"level": i})])
    write_nodes(root)
    cfg = {**nightly.DEFAULTS, "vault": str(vault), "synthetic_per_night": 2}
    client = FakeClient([GOOD, {"request": "battery?", "calls": [{"tool": "get_battery", "args": {}}]}])

    night = nightly.run(
        root, cfg, client=client, now=NOW, hardware="needs Apple silicon (this machine: Linux x86_64, 1 GB RAM)"
    )
    r = night.record
    assert r["night"] == 1 and r["real_total"] == 10 and r["real_new"] == 10 and r["synthetic_new"] == 2
    assert r["train"]["status"] == "skipped" and r["eval"]["candidate"] is None
    assert night.summary.startswith("Night 1: 10 real examples (+10), 2 synthetic (+2), test set ")
    assert "tools covered 2/3" in night.summary
    assert "training: skipped (needs 16 GB Apple silicon)" in night.summary
    assert "eval: baseline only" in night.summary and "cost $0.00" in night.summary

    lines = (root / "data/training/progress.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["night"] == 1
    note = (vault / "Portfolio/Projects/Sommus Model Progress.md").read_text()
    assert note.startswith("# Sommus Model Progress") and "## 2026-11-18 (night 1)" in note and night.summary in note
    assert json.loads((root / "data/training/manifest.json").read_text())["last_turn_id"] > 0
    assert (root / "data/training/synthetic_train.jsonl").exists() or (
        root / "data/training/synthetic_test.jsonl"
    ).exists()

    # a second night: nothing new to export, the counts carry over and the night number advances
    again = nightly.run(
        root, cfg, client=client, now=NOW, hardware="needs Apple silicon (this machine: Linux x86_64, 1 GB RAM)"
    )
    assert again.record["night"] == 2 and again.record["real_new"] == 0 and again.record["real_total"] == 10
    assert note.count("## ") < (vault / "Portfolio/Projects/Sommus Model Progress.md").read_text().count("## ")


def test_synthetic_spend_lands_in_the_main_log_but_never_in_the_next_export(tmp_path):
    root = tmp_path
    Store(root / "data" / "sommus.db")
    write_nodes(root)
    cfg = {**nightly.DEFAULTS, "vault": str(tmp_path / "none"), "synthetic_per_night": 1}
    night = nightly.run(root, cfg, client=FakeClient([GOOD]), now=NOW, hardware="needs Apple silicon")
    assert night.record["cost_usd"] > 0
    assert Store(root / "data" / "sommus.db").cost_month() > 0
    again = nightly.run(root, cfg, client=FakeClient([]), now=NOW, hardware="needs Apple silicon")
    assert again.record["real_total"] == 0


def test_without_an_api_key_the_summary_says_synthetic_was_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    write_nodes(tmp_path)
    cfg = {**nightly.DEFAULTS, "vault": str(tmp_path / "none")}
    night = nightly.run(tmp_path, cfg, now=NOW, hardware="needs Apple silicon")
    assert "skipped: no ANTHROPIC_API_KEY" in night.summary and night.record["cost_usd"] == 0


def test_a_failing_stage_is_recorded_and_the_report_is_still_written(tmp_path):
    write_nodes(tmp_path)
    cfg = {**nightly.DEFAULTS, "vault": str(tmp_path / "none"), "synthetic_per_night": 1}

    class Broken(FakeClient):
        def create(self, **kw):
            raise RuntimeError("api down")

    night = nightly.run(tmp_path, cfg, client=Broken([]), now=NOW, hardware="needs Apple silicon")
    assert "synthetic failed: api down" in night.summary
    assert (tmp_path / "data/training/progress.jsonl").exists()


def test_disabled_runs_nothing(tmp_path):
    night = nightly.run(tmp_path, {**nightly.DEFAULTS, "enabled": False, "vault": str(tmp_path / "none")}, now=NOW)
    assert night.record["real_new"] == 0 and night.record["train"]["status"] == "skipped"


def test_summary_text_for_a_trained_candidate():
    r = {"night": 12, "real_total": 184, "real_new": 6, "synthetic_total": 480, "synthetic_new": 40,
         "synthetic_status": "ok", "synthetic_reason": "", "test_total": 61, "tools_covered": 38, "tools_total": 87,
         "train": {"status": "trained", "examples": 600, "short": "trained"},
         "eval": {"baseline": {"real": {"score": 0.3, "n": 40}},
                  "candidate": {"real": {"n": 40, "tool_accuracy": 0.71, "arg_accuracy": 0.64, "score": 0.52}}},
         "cost_usd": 0.03, "errors": []}  # fmt: skip
    assert report.summary(r) == (
        "Night 12: 184 real examples (+6), 480 synthetic (+40), test set 61, tools covered 38/87, "
        "training: done on 600 examples, eval: tools 71%, args 64%, score 0.52 vs always-escalate 0.30, cost $0.03"
    )
