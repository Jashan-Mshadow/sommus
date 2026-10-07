"""The Mac's live mode as a client of the always-on brain.

`sommus live` used to run its own brain: a second Sommus with its own conversation, PIN lock and spend. Now,
when the server answers, live mode hands every request, tool call and PIN to it over the tailnet (server.py's
/live/* routes), so the laptop, the iPhone and Telegram are one Sommus. Audio still never leaves the Mac except
to Gemini after the wake word, and a PIN said here goes only to the server.

`RemoteBrain` has the parts of `Brain` that live mode uses (handle, call_tool, gate, lock, store), so live.py
doesn't know which one it has. If the server can't be reached at startup, live mode runs its own brain as before.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx2

from sommus import config
from sommus.brain import pin
from sommus.brain.loop import Notice, TextDelta, TurnDone
from sommus.brain.store import Usage


def server_url() -> str:
    settings = config.deploy()
    return f"http://{settings['server_host']}:{settings['api_port']}"


def reachable(timeout: float = 3.0) -> bool:
    try:
        return httpx2.get(f"{server_url()}/health", timeout=timeout).status_code == 200
    except Exception:
        return False


@dataclass
class RemoteResult:
    text: str
    decision: str
    is_error: bool


class RemoteGate:
    """The server's PIN lock, seen from the Mac: state comes back with every answer."""

    def __init__(self, brain: RemoteBrain):
        self._brain = brain
        self._pending: str | None = None
        self._unlocked_until = 0.0
        self._expecting = False
        self.settle = None  # voice ID's wait (not used while it's parked)
        self.active = True

    def update(self, state: dict[str, Any]) -> None:
        self._pending = state.get("pending")
        self._unlocked_until = time.monotonic() + float(state.get("unlocked_for") or 0)
        self._expecting = bool(state.get("expecting"))

    @property
    def pending(self) -> str | None:
        return self._pending

    @pending.setter
    def pending(self, value: str | None) -> None:
        self._pending = value
        if value is None:
            self._brain.post_sync("/live/gate", {"do": "clear_pending"})

    @property
    def unlocked(self) -> bool:
        return time.monotonic() < self._unlocked_until

    @property
    def expecting(self) -> bool:
        return self._expecting

    def claims(self, text: str) -> bool:
        return self._expecting or self._pending is not None or pin.mentions_pin(text)

    def attempt(self, text: str) -> str | None:
        state = self._brain.post_sync("/live/pin", {"text": text})
        return state.get("answer") if state else "The server didn't answer, so the PIN wasn't checked."

    def lock(self) -> None:
        self._unlocked_until = 0.0
        self._brain.post_sync("/live/gate", {"do": "lock"})

    def vouch(self, seconds: float = 0) -> None:
        self._brain.post_sync("/live/gate", {"do": "vouch"})

    def unvouch(self) -> None:
        self._brain.post_sync("/live/gate", {"do": "unvouch"})


class RemoteBrain:
    def __init__(self, store, timeout: float = 120.0):
        import asyncio

        self.url = server_url()
        self.store = store  # live mode still logs what was said here, for voice.log and /cost
        self.lock = asyncio.Lock()
        self.gate = RemoteGate(self)
        self._http = httpx2.AsyncClient(timeout=timeout)
        self._last = ""

    async def aclose(self) -> None:
        await self._http.aclose()

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._http.post(f"{self.url}{path}", json=body)
        response.raise_for_status()
        state = response.json()
        self.gate.update(state)
        return state

    def post_sync(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        try:
            response = httpx2.post(f"{self.url}{path}", json=body, timeout=10)
            response.raise_for_status()
            state = response.json()
            self.gate.update(state)
            return state
        except Exception:
            return None

    async def api_tools(self) -> list[dict[str, Any]]:
        response = await self._http.get(f"{self.url}/live/tools")
        response.raise_for_status()
        return response.json()["tools"]

    async def handle(self, text: str, confirm=None) -> AsyncIterator[Any]:
        try:
            state = await self.post("/live/ask", {"text": text})
            self._last = state.get("reply", "")
            yield TextDelta(self._last)
        except Exception as e:
            yield Notice(f"the always-on brain didn't answer ({type(e).__name__})")
        yield TurnDone(Usage(), 0.0, 0)

    async def call_tool(self, name: str, args: dict[str, Any], asked: str, model: str = "gemini-live") -> RemoteResult:
        try:
            state = await self.post("/live/tool", {"name": name, "args": args, "asked": asked})
        except Exception as e:
            return RemoteResult(f"The always-on brain didn't answer: {type(e).__name__}", "error", True)
        return RemoteResult(state["text"], state["decision"], bool(state["is_error"]))

    def note_unlocked(self, answer: str = "Unlocked.") -> None:
        """The server tells its own brains when a PIN unlocks; nothing to do here."""

    def last_reply(self) -> str:
        return self._last

    def reset(self) -> None:
        self.post_sync("/live/gate", {"do": "new"})
