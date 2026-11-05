"""The Sommus app: `sommus ui` opens a window with an orb, the conversation and a live feed of what Sommus is doing.

It is a small local web app (127.0.0.1 only) so there is nothing to build: the window is Chrome in app mode.
Typed messages go to the always-on brain (`/live/ask`, the same conversation as the phone and voice). The
microphone button runs `sommus live` in the background and shows its output in the feed instead of a terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from sommus import client, config

PORT = int(os.environ.get("SOMMUS_UI_PORT", "8790"))
STATIC = Path(__file__).parent / "ui_static"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
NOISE = re.compile(r"^[\s⠀-⣿|/\\\-·.]*$")  # spinner frames and blank lines


def clean(chunk: str) -> list[str]:
    """Terminal output as feed lines: no colour codes, no spinner redraws, no blanks."""
    lines = []
    for raw in re.split(r"[\r\n]+", ANSI.sub("", chunk)):
        line = raw.strip()
        if line and not NOISE.match(line):
            lines.append(line)
    return lines


def mood(line: str) -> str | None:
    """What a voice-mode line says about the orb: None when it says nothing."""
    low = line.lower()
    if any(w in low for w in ("thinking", "running", "calling")):
        return "thinking"
    if low.startswith(("sommus:", "sommus ")) or "speaking" in low:
        return "speaking"
    if any(w in low for w in ("listening", "awake", "wake")):
        return "listening"
    return None


class Hub:
    """Fan-out of events to every open window, plus the voice process."""

    def __init__(self) -> None:
        self.listeners: set[asyncio.Queue] = set()
        self.voice: asyncio.subprocess.Process | None = None
        self.state = "idle"

    def emit(self, kind: str, **data: Any) -> None:
        for queue in list(self.listeners):
            queue.put_nowait({"kind": kind, **data})

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.emit("state", state=state)

    @property
    def voice_on(self) -> bool:
        return self.voice is not None and self.voice.returncode is None

    def command(self) -> list[str]:
        exe = shutil.which("sommus")
        return (
            [exe, "live"] if exe else [sys.executable, "-c", "from sommus.interfaces.cli import main; main()", "live"]
        )

    async def start_voice(self) -> None:
        if self.voice_on:
            return
        env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "PYTHONUNBUFFERED": "1"}
        self.voice = await asyncio.create_subprocess_exec(
            *self.command(),
            cwd=config.ROOT,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
        self.emit("voice", on=True)
        self.emit("line", text="voice mode starting (loading the microphone and wake word)…", source="system")
        asyncio.create_task(self.pump(self.voice))

    async def pump(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        while chunk := await process.stdout.read(4096):
            for line in clean(chunk.decode("utf-8", "replace")):
                self.emit("line", text=line, source="voice")
                if (felt := mood(line)) is not None:
                    self.set_state(felt)
        await process.wait()
        self.emit("voice", on=False)
        self.emit("line", text=f"voice mode stopped (exit {process.returncode}).", source="system")
        self.set_state("idle")

    async def stop_voice(self) -> None:
        if not self.voice_on:
            return
        assert self.voice is not None
        self.voice.terminate()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.voice.wait(), 8)
        if self.voice.returncode is None:
            self.voice.kill()


def make_app(hub: Hub | None = None, http: httpx2.AsyncClient | None = None) -> Starlette:
    hub = hub or Hub()
    web = http or httpx2.AsyncClient(timeout=180)

    async def index(_request: Request) -> FileResponse:
        return FileResponse(STATIC / "index.html")

    async def status(_request: Request) -> JSONResponse:
        brain = False
        try:
            brain = (await web.get(f"{client.server_url()}/health", timeout=4)).status_code == 200
        except Exception:
            pass
        return JSONResponse({"brain": brain, "voice": hub.voice_on, "state": hub.state})

    async def say(request: Request) -> JSONResponse:
        text = str((await request.json()).get("text", "")).strip()
        if not text:
            return JSONResponse({"error": "empty"}, status_code=400)
        hub.emit("you", text=text)
        hub.set_state("thinking")
        try:
            response = await web.post(f"{client.server_url()}/live/ask", json={"text": text})
            response.raise_for_status()
            body = response.json()
        except Exception as e:
            note = f"The always-on brain didn't answer ({type(e).__name__})."
            hub.emit("sommus", text=note)
            hub.set_state("idle")
            return JSONResponse({"error": note}, status_code=502)
        for tool in body.get("tools", []):
            args = ", ".join(f"{k}={json.dumps(v)}" for k, v in (tool.get("input") or {}).items())
            mark = "✓" if tool.get("ok", True) else "✗"
            hub.emit("line", text=f"$ {tool['name']}({args[:90]}) {mark}", source="tool")
        reply = body.get("reply", "")
        hub.emit("sommus", text=reply)
        hub.set_state("speaking")
        await asyncio.sleep(min(6.0, 0.9 + len(reply) / 40))
        hub.set_state("listening" if hub.voice_on else "idle")
        return JSONResponse({"ok": True})

    async def voice(request: Request) -> JSONResponse:
        on = bool((await request.json()).get("on"))
        await (hub.start_voice() if on else hub.stop_voice())
        return JSONResponse({"voice": hub.voice_on})

    async def events(_request: Request) -> StreamingResponse:
        queue: asyncio.Queue = asyncio.Queue()
        hub.listeners.add(queue)

        async def stream() -> AsyncIterator[str]:
            try:
                yield f"data: {json.dumps({'kind': 'state', 'state': hub.state})}\n\n"
                while True:
                    try:
                        yield f"data: {json.dumps(await asyncio.wait_for(queue.get(), 15))}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                hub.listeners.discard(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        yield
        await hub.stop_voice()

    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/status", status),
            Route("/api/say", say, methods=["POST"]),
            Route("/api/voice", voice, methods=["POST"]),
            Route("/api/events", events),
        ],
        lifespan=lifespan,
    )


def open_window(url: str) -> None:
    """A Chrome app window (no tabs, no address bar), else the default browser."""
    if Path("/Applications/Google Chrome.app").exists():
        subprocess.Popen(["open", "-na", "Google Chrome", "--args", f"--app={url}", "--window-size=1100,760"])
    else:
        webbrowser.open(url)


def run() -> None:
    import uvicorn

    url = f"http://127.0.0.1:{PORT}"
    app = make_app()

    async def main() -> None:
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
        task = asyncio.create_task(server.serve())
        await asyncio.sleep(0.8)
        open_window(url)
        print(f"Sommus app on {url} (Ctrl+C to quit)", flush=True)
        await task

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
