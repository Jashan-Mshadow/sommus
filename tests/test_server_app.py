"""The server's HTTP routes and background loops: bad input and failures stay contained."""

import asyncio
import subprocess

from fakes import config
from starlette.testclient import TestClient

from sommus import server as server_module
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store
from sommus.server import Phone, Server


class Everyone:
    def is_mine(self, address: str) -> bool:
        return True


async def test_a_live_route_given_broken_json_answers_400_not_a_crash(tmp_path):
    async with NodeHub((), Policy()) as hub:
        sommus = Server(config(tmp_path), hub, Store(tmp_path / "t.db"), Phone(), log=lambda _line: None)
        sommus.tailnet = Everyone()
        with TestClient(sommus.app()) as client:
            response = client.post("/live/pin", content=b"{not json", headers={"content-type": "application/json"})
    assert response.status_code == 400


async def test_a_vault_sync_failure_is_logged_and_the_loop_goes_on(tmp_path, monkeypatch):
    calls: list[int] = []
    logged: list[str] = []

    def hung(vault, log):
        calls.append(1)
        raise subprocess.TimeoutExpired(["git", "pull"], 60)

    monkeypatch.setattr(server_module, "sync_vault", hung)
    monkeypatch.setattr(server_module, "VAULT_SYNC_SECONDS", 0)
    async with NodeHub((), Policy()) as hub:
        sommus = Server(config(tmp_path), hub, Store(tmp_path / "t.db"), Phone(), log=logged.append)
        task = asyncio.create_task(sommus.vault_sync())
        while len(calls) < 2:
            await asyncio.sleep(0.01)
        assert not task.done()
        task.cancel()
    assert logged and logged[0].startswith("Vault sync: TimeoutExpired")


def test_sync_vault_commits_local_notes_without_an_upstream(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], capture_output=True, text=True, check=False)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "note.md").write_text("hi\n")
    logged: list[str] = []
    server_module.sync_vault(tmp_path, logged.append)
    assert "note.md" in git("show", "--stat", "HEAD").stdout
