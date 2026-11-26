"""Hard sleep: the phrases, the flag file, and that every channel goes quiet until the wake phrase."""

import asyncio
from types import SimpleNamespace

import pytest
from fakes import config
from starlette.testclient import TestClient

from sommus import sleepmode
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store
from sommus.interfaces import telegram
from sommus.server import Phone, Server
from sommus.sleepmode import Sleep

SLEEP, WAKE = "sleepmax:test-phrase-1", "wakeupmax:test-phrase-1"


@pytest.fixture(autouse=True)
def phrases(monkeypatch):
    monkeypatch.setenv(sleepmode.SLEEP_ENV, sleepmode.digest(SLEEP))
    monkeypatch.setenv(sleepmode.WAKE_ENV, sleepmode.digest(WAKE))


class Everyone:
    def is_mine(self, address: str) -> bool:
        return True


def test_phrases_match_with_or_without_the_name_case_and_spaces():
    stored = sleepmode.digest(SLEEP)
    assert sleepmode.matches("sleepmax:test-phrase-1", stored)
    assert sleepmode.matches("Sommus SLEEPMAX:test-phrase-1", stored)
    assert sleepmode.matches("hey sommus, sleepmax: test-phrase-1.", stored)


def test_near_misses_and_other_text_do_not_match():
    stored = sleepmode.digest(SLEEP)
    for text in ("sleepmax:test-phrase-2", "sleep", "sommus go to sleep", "", "x" * 500, WAKE):
        assert not sleepmode.matches(text, stored)
    assert not sleepmode.matches(SLEEP, "")  # no hash set: nothing matches


def test_the_hash_does_not_contain_the_phrase():
    assert "test-phrase" not in sleepmode.digest(SLEEP)


def test_sleep_then_wake_and_the_state_survives_a_restart(tmp_path):
    sleep = Sleep(tmp_path)
    assert not sleep.asleep
    assert sleep.command("what's the weather") is None
    assert sleep.command(SLEEP, "Telegram") == "sleep"
    assert sleep.asleep
    assert Sleep(tmp_path).asleep  # a new process (a restart, a deploy) finds it still asleep
    assert sleep.command(WAKE) == "wake"
    assert not sleep.asleep and not Sleep(tmp_path).asleep


def test_asleep_only_the_wake_phrase_counts(tmp_path):
    sleep = Sleep(tmp_path)
    sleep.command(SLEEP)
    assert sleep.command(SLEEP) is None  # the sleep phrase again does nothing
    assert sleep.command("hello") is None and sleep.command("sleepmax:wrong") is None
    assert sleep.asleep
    assert sleep.command(WAKE) == "wake"


def test_awake_the_wake_phrase_is_just_text(tmp_path):
    assert Sleep(tmp_path).command(WAKE) is None


def test_with_no_phrases_set_the_feature_is_off(tmp_path, monkeypatch):
    monkeypatch.delenv(sleepmode.SLEEP_ENV)
    assert Sleep(tmp_path).command(SLEEP) is None


def test_the_mac_copies_the_servers_state(tmp_path):
    sleep = Sleep(tmp_path)
    sleep.mirror(True)
    assert sleep.asleep and Sleep(tmp_path).asleep
    sleep.mirror(False)
    assert not sleep.asleep


class FakeBot(telegram.Bot):
    def __init__(self, tmp_path, texts):
        self.allowed, self.offset, self.seen_strangers, self.failures = {42}, None, set(), 0
        self.sleep = Sleep(tmp_path)
        self.brain = SimpleNamespace(gate=SimpleNamespace(active=False))
        self.updates = [
            {"update_id": i, "message": {"message_id": i, "chat": {"id": 42}, "from": {}, "text": text}}
            for i, text in enumerate(texts, 1)
        ]
        self.said, self.deleted, self.reached = [], [], []

    async def poll(self, log=None):
        found, self.updates = self.updates, []
        if not found:
            raise StopAsyncIteration
        return found

    async def say(self, chat, text):
        self.said.append(text)

    async def call(self, method, **params):
        if method == "deleteMessage":
            self.deleted.append(params["message_id"])

    async def handle(self, chat, text, log):
        self.reached.append(text)


async def test_telegram_goes_silent_when_asleep_and_wakes_on_the_phrase(tmp_path):
    bot = FakeBot(tmp_path, ["hi", SLEEP, "are you there?", "wakeupmax:wrong", "/cost", WAKE, "hi again"])
    with pytest.raises(StopAsyncIteration):
        await bot.run(lambda _line: None)
    assert bot.reached == ["hi", "hi again"]  # nothing in between reached the brain
    assert bot.said == ["Going to sleep.", "Awake."]  # the only two words said, one per phrase
    assert bot.deleted == [2, 6]  # both phrases removed from the chat


async def test_the_server_is_silent_while_asleep_and_wakes_on_the_phrase(tmp_path):
    async with NodeHub((), Policy()) as hub:
        sommus = Server(config(tmp_path), hub, Store(tmp_path / "t.db"), Phone(), log=lambda _line: None)
        sommus.tailnet = Everyone()
        told = []

        async def fake_bot_say(chat, text):
            told.append(text)

        sommus.bot = SimpleNamespace(allowed={1}, say=fake_bot_say)
        with TestClient(sommus.app()) as client:
            assert client.get("/health").json()["asleep"] is False
            assert client.post("/ask", json={"text": SLEEP}).json() == {"reply": "Going to sleep.", "action": "sleep"}
            assert sommus.sleep.asleep and client.get("/health").json()["asleep"] is True

            # the iPhone: silent, even for a wrong phrase; the Mac's routes refuse; nudges are dropped
            assert client.post("/ask", json={"text": "turn off the lamp"}).json() == {"reply": "", "action": "sleep"}
            assert client.post("/ask", json={"text": "wakeupmax:nope"}).json() == {"reply": "", "action": "sleep"}
            assert client.post("/live/ask", json={"text": "hi"}).status_code == 423
            assert client.post("/live/tool", json={"name": "x", "args": {}, "asked": "x"}).status_code == 423
            assert client.get("/live/tools").status_code == 200  # the Mac needs it to start while asleep
            await sommus.tell("a nudge")
            assert told == []

            # the Mac's typed line can wake it
            assert client.post("/live/sleep", json={"text": "hello"}).json() == {"changed": None, "asleep": True}
            assert client.post("/live/sleep", json={"text": WAKE}).json() == {"changed": "wake", "asleep": False}
            await sommus.tell("a nudge")
            assert told == ["a nudge"]


async def test_the_scheduler_does_nothing_while_asleep(tmp_path, monkeypatch):
    from sommus import server as server_module

    monkeypatch.setattr(server_module, "TICK_SECONDS", 0)
    async with NodeHub((), Policy()) as hub:
        sommus = Server(config(tmp_path), hub, Store(tmp_path / "t.db"), Phone(), log=lambda _line: None)
        ran = []

        async def tick(now):
            ran.append("tick")

        async def diary(now):
            ran.append("diary")

        sommus.tick, sommus.write_diary = tick, diary
        sommus.sleep.command(SLEEP)
        task = asyncio.create_task(sommus.scheduler())
        await asyncio.sleep(0.05)
        assert ran == []
        sommus.sleep.command(WAKE)
        await asyncio.sleep(0.05)
        task.cancel()
        assert "tick" in ran and "diary" in ran
