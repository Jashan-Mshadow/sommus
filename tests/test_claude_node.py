"""Claude node: runs Claude Code on the subscription and turns its JSON into a short answer."""

import json
import subprocess
from datetime import datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from sommus.nodes.claude import server


def test_the_api_key_never_reaches_claude_code(monkeypatch):
    """With the key in its environment the CLI bills the API instead of the subscription."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token")
    monkeypatch.setenv("HOME", "/Users/test")
    env = server.subscription_env()
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env and env["HOME"] == "/Users/test"


def test_the_task_is_dated_and_runs_headless(monkeypatch):
    monkeypatch.setattr(server, "claude_binary", lambda: "claude")
    argv = server.command("what are my classes tomorrow", datetime(2026, 9, 15, 14, 30))
    assert argv[:3] == ["claude", "-p", "Tuesday 2026-09-15, 2:30 PM. Jashan asked: what are my classes tomorrow"]
    assert argv[argv.index("--output-format") + 1] == "json"


def test_the_answer_comes_back_as_plain_text():
    reply = json.dumps({"result": " ECE 150 at 8:30 in PSE 5353. ", "is_error": False})
    assert server.read_reply(reply) == "ECE 150 at 8:30 in PSE 5353."


@pytest.mark.parametrize(
    "stdout", [json.dumps({"result": "", "is_error": True, "subtype": "error_max_turns"}), "not json", ""]
)
def test_failures_explain_themselves(stdout):
    with pytest.raises(ToolError, match="Claude Code"):
        server.read_reply(stdout)


def test_a_slow_task_is_stopped(monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired("claude", server.TIMEOUT_SECONDS)

    monkeypatch.setattr(server, "claude_binary", lambda: "claude")
    monkeypatch.setattr(server.subprocess, "run", slow)
    with pytest.raises(ToolError, match="longer than 5 minutes"):
        server.ask_claude("reorganise my whole drive")
