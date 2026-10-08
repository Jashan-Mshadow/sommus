"""`sommus serve`: the always-on brain, on the Oracle free server.

One process holds the brain and everything that should work with the laptop closed: Telegram, Gmail, web
search, the vault (a clone of its private repo, synced by git), the morning brief and the nudges, and /ask for
the iPhone's Action Button. The Mac's tools arrive over the tailnet from `sommus node` (remote.py); while the
Mac sleeps they answer "the laptop is asleep" instead of vanishing.

/ask listens only on the server's tailnet address, and every caller is checked with `tailscale whois`: it must
be one of Jashan's own devices. So the phone needs no token, and nothing on the public internet can reach it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sommus import brief, config
from sommus.brain import campus, fastpath
from sommus.brain.loop import Brain, Notice, TextDelta, TurnDone
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store
from sommus.contacts import ContactBook
from sommus.reminders import Reminders

VAULT_SYNC_SECONDS = 300
TICK_SECONDS = 30
CONTACTS_EVERY_SECONDS = 6 * 3600  # recopy the Mac's contacts this often, when it's awake


# ---------------------------------------------------------------- the phone's own tools


class Phone:
    """Tools that run on the iPhone itself. The brain can't reach into a phone, so a tool here only queues the
    action; /ask hands it back and the Shortcut does it (flashlight, a text from the phone, an alarm)."""

    def __init__(self) -> None:
        self.present = False  # true only while a request from the iPhone is being handled
        self.action: dict[str, Any] = {}

    def node(self):
        from mcp.server.mcpserver import MCPServer
        from mcp.types import ToolAnnotations

        server = MCPServer("phone", log_level="WARNING")
        quick = ToolAnnotations(read_only_hint=False, destructive_hint=False)

        def queue(action: dict[str, Any], done: str) -> str:
            if not self.present:
                return "This only works when Jashan is talking through his iPhone (the Action Button)."
            if self.action:
                return f"The phone can do one thing per request; already queued: {self.action['action']}."
            self.action = action
            return done

        @server.tool(annotations=quick, structured_output=False)
        def phone_flashlight(on: bool = True) -> str:
            """Turn the iPhone's flashlight on or off. Only when the request came from the iPhone."""
            action = "flashlight_on" if on else "flashlight_off"
            return queue({"action": action}, "Flashlight " + ("on." if on else "off."))

        @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True), structured_output=False)
        def phone_text(to: str, message: str) -> str:
            """Send an iMessage/SMS from the iPhone. Only when the request came from the iPhone.

            Args:
                to: A phone number (look a name up with find_contact first) or an email for iMessage.
                message: The text to send, as Jashan would write it.
            """
            return queue({"action": "text", "to": to, "message": message}, f"Texting {to} from the phone.")

        @server.tool(annotations=quick, structured_output=False)
        def phone_alarm(time_24h: str, label: str = "") -> str:
            """Set an alarm on the iPhone. Only when the request came from the iPhone.

            Args:
                time_24h: "07:30" style.
                label: Optional alarm name.
            """
            return queue({"action": "alarm", "time": time_24h, "label": label}, f"Alarm set for {time_24h}.")

        return server


# ---------------------------------------------------------------- who's asking


class Tailnet:
    """Is this address one of Jashan's devices? Asked of tailscaled, cached for ten minutes."""

    def __init__(self) -> None:
        self._owner: str | None = None
        self._seen: dict[str, tuple[bool, float]] = {}

    def _run(self, *args: str) -> dict:
        # --json before the address: Go's flag parsing stops at the first plain argument.
        done = subprocess.run(["tailscale", args[0], "--json", *args[1:]], capture_output=True, text=True, timeout=5)
        return json.loads(done.stdout) if done.returncode == 0 else {}

    def owner(self) -> str:
        """The tailnet user this server belongs to: Jashan."""
        if not self._owner:
            self._owner = str(self._run("status").get("Self", {}).get("UserID", ""))
        return self._owner

    def is_mine(self, address: str) -> bool:
        seen = self._seen.get(address)
        if seen and time.monotonic() - seen[1] < 600:
            return seen[0]
        try:
            who = str(self._run("whois", address).get("Node", {}).get("User", ""))
            mine = bool(who) and who == self.owner()
        except Exception:
            mine = False
        self._seen[address] = (mine, time.monotonic())
        return mine


# ---------------------------------------------------------------- the server


class Server:
    def __init__(
        self,
        cfg: config.Config,
        hub: NodeHub,
        store: Store,
        phone: Phone,
        log=print,
        reminders: Reminders | None = None,
        contacts: ContactBook | None = None,
    ):
        self.cfg, self.hub, self.store, self.phone, self.log = cfg, hub, store, phone, log
        self.reminders, self.contacts = reminders, contacts
        # One Sommus: Telegram (written replies), the iPhone (spoken) and the Mac's live mode (relayed through
        # Gemini) each get the system prompt that fits, but share one conversation, one PIN lock and one lock
        # on turns. Ask on the phone, follow up on the laptop.
        self.brain = Brain(cfg, hub, store)
        self.voice_brain = Brain(cfg, hub, store, voice=True)
        self.voice_brain.channel = "iPhone (Action Button); the reply is spoken"
        self.relay_brain = Brain(cfg, hub, store, relay=True)
        self.relay_brain.channel = "MacBook (live voice)"
        for other in (self.voice_brain, self.relay_brain):
            other.messages, other.gate, other.lock = self.brain.messages, self.brain.gate, self.brain.lock
        self.bot = None
        self.tailnet = Tailnet()
        self.state_path = cfg.data_dir / "brief_state.json"
        self.state = self._load_state()

    def _load_state(self) -> dict:
        with contextlib.suppress(OSError, ValueError):
            return json.loads(self.state_path.read_text())
        return {}

    def _save_state(self) -> None:
        with contextlib.suppress(OSError):
            self.state_path.write_text(json.dumps(self.state))

    # -- the iPhone

    async def ask(self, text: str) -> dict[str, Any]:
        reply: list[str] = []
        notices: list[str] = []

        async def allow(name: str, args: dict) -> bool:
            return True

        async with self.brain.lock:
            self.phone.present, self.phone.action = True, {}
            try:
                async for event in self.voice_brain.handle(text, allow):
                    match event:
                        case TextDelta(text=delta):
                            reply.append(delta)
                        case Notice(text=notice):
                            notices.append(notice)
                        case TurnDone():
                            pass
            finally:
                self.phone.present = False
        said = "".join(reply).strip() or "Done."
        return {"reply": said, **({"action": ""} | self.phone.action), "notices": notices}

    def app(self):
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def ask(request: Request) -> JSONResponse:
            caller = request.client.host if request.client else ""
            if not await asyncio.to_thread(self.tailnet.is_mine, caller):
                self.log(f"Refused /ask from {caller}: not one of Jashan's devices")
                return JSONResponse({"reply": "Not allowed."}, status_code=403)
            raw = await request.body()
            try:
                body = json.loads(raw)
            except ValueError:
                body = raw.decode(errors="replace")
            if isinstance(body, dict):
                # The Shortcut's JSON key is easy to leave blank or misspell: take "text", else any string in it.
                text = str(body.get("text") or next((v for v in body.values() if isinstance(v, str) and v.strip()), ""))
            else:
                text = str(body)
            text = text.strip()
            if not text:
                self.log(f"/ask got no words: {len(raw)} bytes, {raw[:120]!r}")
                # Silence in the Shortcut's loop means he's done: end it rather than ask again and again.
                return JSONResponse({"reply": "Going to sleep.", "action": "sleep"})
            if fastpath.DISMISS.match(text):  # "you're good to go": the Shortcut stops listening
                return JSONResponse({"reply": "Going to sleep.", "action": "sleep"})
            try:
                result = await self.ask(text)
            except Exception as e:
                self.log(f"/ask failed: {type(e).__name__}: {e}")
                result = {"reply": "Something went wrong on my end.", "action": ""}
            self.log(f"iPhone: {text[:50]!r} → {result.get('action') or 'reply'}")
            return JSONResponse(result)

        async def health(_request: Request) -> JSONResponse:
            mac = all(self.hub.reachable(n.name) for n in self.cfg.nodes if n.where == "mac")
            return JSONResponse({"ok": True, "mac": mac})

        def mine(handler):
            async def checked(request: Request) -> JSONResponse:
                caller = request.client.host if request.client else ""
                if not await asyncio.to_thread(self.tailnet.is_mine, caller):
                    self.log(f"Refused {request.url.path} from {caller}")
                    return JSONResponse({"error": "not allowed"}, status_code=403)
                body = await request.json() if request.method == "POST" else {}
                try:
                    return JSONResponse(await handler(body if isinstance(body, dict) else {}))
                except Exception as e:
                    self.log(f"{request.url.path} failed: {type(e).__name__}: {e}")
                    return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)

            return checked

        routes = [
            Route("/ask", ask, methods=["POST"]),
            Route("/health", health),
            Route("/live/tools", mine(self.live_tools)),
            Route("/live/ask", mine(self.live_ask), methods=["POST"]),
            Route("/live/tool", mine(self.live_tool), methods=["POST"]),
            Route("/live/pin", mine(self.live_pin), methods=["POST"]),
            Route("/live/gate", mine(self.live_gate), methods=["POST"]),
        ]
        return Starlette(routes=routes)

    # -- the Mac's live mode, as a client of this brain (client.py)

    def gate_state(self) -> dict[str, Any]:
        gate = self.brain.gate
        left = max(0.0, gate.unlocked_until - gate.clock()) if gate.unlocked else 0.0
        return {"pending": gate.pending, "unlocked_for": left, "expecting": gate.expecting}

    async def live_tools(self, _body: dict) -> dict[str, Any]:
        return {"tools": self.hub.api_tools()}

    async def live_ask(self, body: dict) -> dict[str, Any]:
        parts: list[str] = []

        async def allow(name: str, args: dict) -> bool:
            return True

        async with self.brain.lock:
            async for event in self.relay_brain.handle(str(body.get("text", "")), allow):
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                elif isinstance(event, Notice):
                    parts.append(f" ({event.text})")
        return {"reply": "".join(parts).strip(), **self.gate_state()}

    async def live_tool(self, body: dict) -> dict[str, Any]:
        result = await self.relay_brain.call_tool(str(body["name"]), dict(body.get("args") or {}), str(body["asked"]))
        return {"text": result.text, "decision": result.decision, "is_error": result.is_error, **self.gate_state()}

    async def live_pin(self, body: dict) -> dict[str, Any]:
        """A PIN heard or typed on the Mac (never sent to Google): checked here, where the lock is."""
        gate = self.brain.gate
        answer = gate.attempt(str(body.get("text", "")))
        if gate.unlocked:
            for brain in (self.brain, self.voice_brain, self.relay_brain):
                brain.note_unlocked()
        return {"answer": answer, **self.gate_state()}

    async def live_gate(self, body: dict) -> dict[str, Any]:
        gate = self.brain.gate
        match body.get("do"):
            case "clear_pending":
                gate.pending = None
            case "lock":
                gate.lock()
            case "vouch":
                gate.vouch()
            case "unvouch":
                gate.unvouch()
            case "new":
                self.brain.reset()
        return self.gate_state()

    # -- what Sommus says first

    async def tell(self, text: str) -> None:
        if not self.bot:
            return
        for chat in self.bot.allowed:
            with contextlib.suppress(Exception):
                await self.bot.say(chat, text)

    async def _mail(self) -> str:
        with contextlib.suppress(Exception):
            query = {"query": "is:unread category:primary newer_than:1d", "limit": 10}
            found = await self.hub.call("search_email", query)
            if not found.is_error:
                return brief.unread_summary(found.text)
        return ""

    def _weather(self) -> str:
        home = config.section("fastpath")
        with contextlib.suppress(Exception):
            return fastpath.weather_answer(
                ["weather", "today"],
                home.get("place", "Waterloo"),
                float(home.get("latitude", 43.4643)),
                float(home.get("longitude", -80.5204)),
            )
        return ""

    async def morning_brief(self, now: datetime) -> str:
        schedule = campus.cached(campus.schedule_path())
        weather = await asyncio.to_thread(self._weather)
        ranked, _added = campus.task_sections(campus.todo_path())
        text = brief.morning(schedule, now, weather, await self._mail(), ranked)
        if self.reminders and (today := brief.reminders_today(self.reminders.open(), now)):
            text += "\n" + today
        return text

    async def tick(self, now: datetime) -> None:
        settings = config.section("brief")
        try:
            schedule = campus.cached(campus.schedule_path())
        except Exception:
            return
        now = now.astimezone(schedule.zone)
        today = now.date().isoformat()
        morning = brief.due_at(settings.get("morning", "07:00"), now.date(), schedule.zone)
        if now >= morning and self.state.get("brief") != today and (now - morning).total_seconds() < 3 * 3600:
            self.state["brief"] = today
            self._save_state()
            await self.tell(await self.morning_brief(now))
        evening = brief.due_at(settings.get("evening", "20:00"), now.date(), schedule.zone)
        if now >= evening and self.state.get("evening") != today and (now - evening).total_seconds() < 3600:
            self.state["evening"] = today
            self._save_state()
            if heads_up := brief.due_tomorrow(schedule, now):
                await self.tell(heads_up)
        sent = {tuple(k) for k in self.state.get("nudged", [])}
        for line in brief.class_nudges(schedule, now, int(settings.get("nudge_minutes", 15)), sent):
            await self.tell(line)
        self.state["nudged"] = [list(k) for k in sorted(sent)][-50:]
        self._save_state()

    async def fire_reminders(self, now: datetime) -> None:
        if not self.reminders:
            return
        for reminder, late in self.reminders.take_due(now):
            note = f" (meant for {reminder.when():%-I:%M %p})" if late else ""
            await self.tell(f"⏰ {reminder.title}{note}")
            self.log(f"Reminder sent: {reminder.title}")

    async def copy_contacts(self) -> None:
        """While the Mac is awake, refresh the server's copy of its Contacts."""
        if not self.contacts or not self.hub.reachable("laptop"):
            return
        listing = await self.hub.call_on("laptop", "list_contacts", {})
        if not listing.is_error and (count := self.contacts.replace(listing.text, datetime.now())):
            self.log(f"Copied {count} contacts from the Mac")
            self.state["contacts_at"] = time.time()
            self._save_state()

    async def scheduler(self) -> None:
        while True:
            now = datetime.now().astimezone()
            try:
                await self.tick(now)
                await self.fire_reminders(now.replace(tzinfo=None))
                if time.time() - self.state.get("contacts_at", 0) > CONTACTS_EVERY_SECONDS:
                    await self.copy_contacts()
            except Exception as e:
                self.log(f"Scheduler: {type(e).__name__}: {e}")
            await asyncio.sleep(TICK_SECONDS)

    # -- the vault

    async def vault_sync(self) -> None:
        """The vault here is a clone: pull what the Mac pushed, push what Sommus wrote (to-dos, memories)."""
        vault = Path(os.environ.get("SOMMUS_VAULT_PATH", "~/Documents/Jashans_Brain")).expanduser()
        while True:
            await asyncio.to_thread(sync_vault, vault, self.log)
            await asyncio.sleep(VAULT_SYNC_SECONDS)


def sync_vault(vault: Path, log=print) -> None:
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(vault), *args], capture_output=True, text=True, timeout=60)

    if not (vault / ".git").exists():
        return
    if git("status", "--porcelain").stdout.strip():
        git("add", "-A")
        git("commit", "-q", "-m", "Sommus: notes from the always-on brain")
    pulled = git("pull", "--rebase", "--autostash", "-q")
    if pulled.returncode != 0:
        git("rebase", "--abort")
        log(f"Vault pull failed, left as is: {pulled.stderr.strip()[:200]}")
        return
    if git("status", "-sb").stdout.splitlines()[0].find("ahead") != -1:
        pushed = git("push", "-q")
        if pushed.returncode != 0:
            log(f"Vault push failed: {pushed.stderr.strip()[:200]}")


async def serve(log=print) -> None:
    import uvicorn

    from sommus.interfaces import telegram as tg

    cfg = config.load()
    store = Store(cfg.data_dir / "sommus.db")
    phone = Phone()
    settings = config.deploy()
    async with NodeHub(cfg.nodes, Policy(cfg.overrides)) as hub:
        from mcp import Client

        reminders = Reminders(cfg.data_dir / "reminders.json")
        contacts = ContactBook(cfg.data_dir / "contacts.txt")
        await hub.add("phone", Client(phone.node()))
        await hub.add("reminders", Client(reminders.node()))
        await hub.add("contacts", Client(contacts.node()))
        from sommus import calendars

        if links := calendars.urls():  # CALENDAR_ICS_URLS in .env: Google's secret iCal addresses
            await hub.add("calendar", Client(calendars.Calendars(links).node()))
        for name, why in hub.unreachable.items():
            log(f"Node '{name}': {why}")
        server = Server(cfg, hub, store, phone, log, reminders, contacts)
        tasks = [asyncio.create_task(server.scheduler()), asyncio.create_task(server.vault_sync())]
        # SOMMUS_TELEGRAM=off: a test run while a Mac session still polls the bot (two pollers break both).
        if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("SOMMUS_TELEGRAM") != "off":
            token, allowed = tg.credentials()
            server.bot = tg.Bot(token, allowed, server.brain, store, cfg)
            log(f"Telegram: @{await server.bot.whoami()} for {len(allowed)} chat(s)")
            tasks.append(asyncio.create_task(server.bot.run(log)))
        host, port = settings["server_host"], int(settings["api_port"])
        api = uvicorn.Server(uvicorn.Config(server.app(), host=host, port=port, log_level="warning"))
        tasks.append(asyncio.create_task(api.serve()))
        log(f"Sommus is up: {len(hub.api_tools())} tools · /ask on {settings['server_host']}:{settings['api_port']}")
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            if server.bot:
                await server.bot.close()
