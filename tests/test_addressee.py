"""Telling a follow-up for Sommus from talk to someone else in the room."""

from types import SimpleNamespace

import anthropic
import httpx2

from sommus.interfaces import addressee


class FakeClient:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.sent = answer, error, []
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **request):
        self.sent.append(request)
        if self.error:
            raise self.error
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.answer)])


async def test_chatter_to_a_friend_is_ignored():
    client = FakeClient("no")
    heard = "Have you seen Jarvis from Iron Man? I'm trying to build that right now."
    assert not await addressee.said_to_sommus(client, "Jashan", "You're at 70%.", heard)
    assert "You're at 70%." in client.sent[0]["messages"][0]["content"]  # judged against what Sommus just said


async def test_a_follow_up_goes_through():
    assert await addressee.said_to_sommus(FakeClient("Yes"), "Jashan", "Muted.", "turn it back on")


async def test_when_the_check_fails_the_request_still_runs():
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    assert await addressee.said_to_sommus(FakeClient(error=error), "Jashan", "", "what's the time")
