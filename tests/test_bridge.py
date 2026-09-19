"""AI bridge: CLI output parsing and safety defaults, with no real Gemini or Codex calls."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from sommus.nodes.bridge import server as bridge


def test_gemini_json_reply_is_unwrapped():
    out = "Loaded cached credentials.\n" + json.dumps({"response": "Reading week is Oct 12–16.", "stats": {}})
    assert bridge.parse_gemini(out, "", 0) == "Reading week is Oct 12–16."


def test_gemini_not_signed_in_says_how_to_fix():
    with pytest.raises(ToolError, match="aistudio.google.com"):
        bridge.parse_gemini("Please set an Auth method in your settings.json", "", 41)


def test_gemini_error_object_is_reported():
    with pytest.raises(ToolError, match="quota exceeded"):
        bridge.parse_gemini(json.dumps({"error": {"message": "quota exceeded"}}), "", 1)


def test_codex_answer_file_wins_and_long_replies_are_clipped():
    assert bridge.parse_codex("An answer.", "noise", 0) == "An answer."
    long = bridge.parse_codex("x" * (bridge.MAX_REPLY_CHARS + 10), "", 0)
    assert long.endswith("[truncated]")


@pytest.mark.parametrize(
    ("output", "match"),
    [
        ("ERROR: not logged in", "Sign in with ChatGPT"),
        ("You've hit your usage limit. Try again later.", "allowance is used up"),
        ('ERROR: {"message": "model not supported"}', "Codex error"),
    ],
)
def test_codex_failures_are_explained(output, match):
    with pytest.raises(ToolError, match=match):
        bridge.parse_codex("", output, 1)


def test_billing_keys_never_reach_the_clis(monkeypatch):
    for key in bridge.BILLING_VARS:
        monkeypatch.setenv(key, "sk-should-not-leak")
    env = bridge.cli_env("/opt/node/bin/gemini")
    assert not set(bridge.BILLING_VARS) & set(env)
    assert env["PATH"].startswith("/opt/node/bin:")


def test_files_must_be_full_existing_paths(tmp_path):
    pdf = tmp_path / "L3.pdf"
    pdf.write_bytes(b"%PDF")
    assert bridge.resolve_files([str(pdf)]) == [pdf.resolve()]
    with pytest.raises(ToolError, match="full path"):
        bridge.resolve_files(["L3.pdf"])
    with pytest.raises(ToolError, match="No file"):
        bridge.resolve_files([str(tmp_path / "missing.pdf")])


def test_gemini_runs_read_only_in_an_empty_folder(monkeypatch, tmp_path):
    seen = {}

    class Done:
        stdout, stderr, returncode = json.dumps({"response": "ok"}), "", 0

    def fake_run(command, cwd, **kwargs):
        seen["command"], seen["cwd"] = command, cwd
        return Done()

    monkeypatch.setattr(bridge, "find_cli", lambda name: "/bin/gemini")
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    pdf = tmp_path / "L3.pdf"
    pdf.write_bytes(b"%PDF")
    assert bridge.ask_gemini("summarise", [str(pdf)]) == "ok"
    command = seen["command"]
    assert command[command.index("--approval-mode") + 1] == "plan"
    assert command[command.index("--include-directories") + 1] == str(tmp_path.resolve())
    assert str(pdf.resolve()) in command[command.index("-p") + 1]
    assert seen["cwd"] != str(tmp_path) and "bridge-" in seen["cwd"]
