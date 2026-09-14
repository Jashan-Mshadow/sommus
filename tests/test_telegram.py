"""Telegram interface: who is allowed to talk to it, and message splitting."""

import pytest

from sommus.interfaces import telegram


def test_long_replies_are_split_on_newlines():
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    chunks = telegram.split(text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks).replace(" ", "") == text.replace("\n", "").replace(" ", "")


def test_a_reply_with_no_newlines_is_still_split():
    assert all(len(c) <= 100 for c in telegram.split("y" * 450, limit=100))


def test_an_empty_reply_never_sends_nothing():
    assert telegram.split("   ") == ["(no reply)"]


def test_credentials_require_a_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(telegram.TelegramError, match="BotFather"):
        telegram.credentials()


def test_allowed_ids_are_parsed_as_numbers(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_ALLOWED_IDS", "111, 222")
    token, allowed = telegram.credentials()
    assert token == "123:abc" and allowed == {111, 222}


def test_no_allowed_ids_means_nobody_is_allowed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("TELEGRAM_ALLOWED_IDS", raising=False)
    assert telegram.credentials()[1] == set()


async def test_strangers_are_ignored_and_reported(tmp_path):
    """Anyone who can message the bot could otherwise drive the laptop."""
    logged, replies = [], []

    class FakeBot(telegram.Bot):
        def __init__(self):
            self.allowed = {42}
            self.offset = None
            self.seen_strangers = set()
            self.updates = [
                {"update_id": 1, "message": {"chat": {"id": 99}, "from": {"username": "stranger"}, "text": "hi"}},
                {"update_id": 2, "message": {"chat": {"id": 42}, "from": {"username": "jashan"}, "text": "hi"}},
            ]

        async def poll(self):
            found, self.updates = self.updates, []
            if not found:
                raise StopAsyncIteration
            return found

        async def handle(self, chat, text, log):
            replies.append((chat, text))

    bot = FakeBot()
    with pytest.raises(StopAsyncIteration):
        await bot.run(logged.append)

    assert replies == [(42, "hi")]  # the stranger never reached the brain
    assert any("99" in line and "TELEGRAM_ALLOWED_IDS" in line for line in logged)
