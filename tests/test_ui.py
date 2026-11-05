"""The Sommus app window: its server, the voice-process feed, and what it does when the brain is down."""

import httpx2
from starlette.testclient import TestClient

from sommus.interfaces.ui import Hub, clean, make_app, mood


def test_terminal_output_becomes_clean_feed_lines():
    raw = "\x1b[1;35mSommus\x1b[0m listening\r⠋ Starting nodes\n\n   \nGemini Live connected\n"
    assert clean(raw) == ["Sommus listening", "⠋ Starting nodes", "Gemini Live connected"]


def test_spinner_only_lines_are_dropped():
    assert clean("⠋\r⠙\r⠹\n") == []


def test_what_a_line_says_about_the_orb():
    assert mood("Listening for the wake word") == "listening"
    assert mood("Thinking about it") == "thinking"
    assert mood("something unrelated") is None


def test_the_page_and_status_load_and_a_down_brain_is_reported_not_crashed():
    def refuse(_request):
        raise httpx2.ConnectError("down")

    web = httpx2.AsyncClient(transport=httpx2.MockTransport(refuse))
    with TestClient(make_app(Hub(), web)) as page:
        assert "SOMMUS" in page.get("/").text
        assert page.get("/api/status").json() == {"brain": False, "voice": False, "state": "idle"}
        said = page.post("/api/say", json={"text": "hello"})
        assert said.status_code == 502 and "didn't answer" in said.json()["error"]
        assert page.post("/api/say", json={"text": "  "}).status_code == 400


def test_a_reply_and_its_tools_reach_the_feed():
    def brain(request):
        assert request.url.path == "/live/ask"
        return httpx2.Response(
            200, json={"reply": "Done.", "tools": [{"name": "get_battery", "input": {}, "ok": True}]}
        )

    hub = Hub()
    seen = []
    hub.emit = lambda kind, **data: seen.append((kind, data))  # type: ignore[method-assign]
    web = httpx2.AsyncClient(transport=httpx2.MockTransport(brain))
    with TestClient(make_app(hub, web)) as page:
        assert page.post("/api/say", json={"text": "battery?"}).json() == {"ok": True}
    kinds = [k for k, _ in seen]
    assert ("line", {"text": "$ get_battery() ✓", "source": "tool"}) in seen
    assert ("sommus", {"text": "Done."}) in seen and "you" in kinds
