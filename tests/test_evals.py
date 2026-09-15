"""The eval runner: scoring rules, and that simulated tools never actually run."""

from fakes import FakeModel, build_node, config, text_reply, tool_call
from mcp import Client

from sommus import evals
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store


def result(expect, called):
    return evals.Result(case=evals.Case(text="t", expect=tuple(expect)), called=list(called))


def test_passes_when_every_expected_tool_was_called():
    assert result(["peek"], ["peek"]).passed
    assert result(["peek", "wipe"], ["wipe", "peek"]).passed


def test_extra_tools_are_reported_but_still_pass():
    r = result(["peek"], ["peek", "wipe"])
    assert r.passed and r.extra == ["wipe"] and r.missing == []


def test_fails_on_a_missing_tool():
    r = result(["peek", "wipe"], ["peek"])
    assert not r.passed and r.missing == ["wipe"]


def test_a_command_expecting_nothing_fails_if_any_tool_ran():
    assert result([], []).passed
    assert not result([], ["peek"]).passed


def test_the_shipped_commands_file_parses():
    cases = evals.load_cases()
    assert len(cases) >= 20
    assert all(case.text for case in cases)


async def test_non_read_tools_are_simulated_not_run(tmp_path):
    node, calls = build_node()
    async with NodeHub((), Policy()) as real_hub:
        await real_hub.add("test", Client(node))
        hub = evals.SimulatingHub(real_hub)
        model = FakeModel(tool_call("wipe", {"target": "disk"}), text_reply("Wiped."))

        r = await evals.run_case(
            config(tmp_path), hub, Store(tmp_path / "t.db"), evals.Case("wipe the disk", ("wipe",)), client=model
        )

        assert r.passed and r.called == ["wipe"]
        assert calls == []  # the destructive tool was never executed


async def test_read_tools_still_run_for_real(tmp_path):
    node, calls = build_node()
    async with NodeHub((), Policy()) as real_hub:
        await real_hub.add("test", Client(node))
        hub = evals.SimulatingHub(real_hub)
        model = FakeModel(tool_call("peek", {}), text_reply("All quiet."))
        r = await evals.run_case(
            config(tmp_path),
            hub,
            Store(tmp_path / "t.db"),
            evals.Case("anything happening?", ("peek",)),
            client=model,
        )
        assert calls == ["peek"] and hub.executed == ["peek"] and r.passed


async def test_results_are_saved_as_json(tmp_path):
    cfg = config(tmp_path)
    r = result(["peek"], ["peek"])
    path = evals.save([r], live=False, cfg=cfg)
    assert path.exists()
    import json

    saved = json.loads(path.read_text())
    assert saved["passed"] == 1 and saved["total"] == 1 and saved["cases"][0]["called"] == ["peek"]


def test_an_open_ended_case_passes_on_any_answer():
    case = evals.Case(text="connect my speaker", expect=None)
    assert evals.Result(case=case, called=["open_file"]).passed
    assert evals.Result(case=case, called=[]).passed
    assert not evals.Result(case=case, notices=["API error 500"]).passed


def test_server_side_helpers_are_not_counted_as_extra_tools():
    case = evals.Case(text="weather?", expect=("web_search",))
    r = evals.Result(case=case, called=["code_execution", "web_search", "code_execution"])
    assert r.passed and r.extra == []
