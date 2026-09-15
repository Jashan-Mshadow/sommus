"""Telegram interface: text Sommus from anywhere.

The brain is untouched — this is another interface consuming the same event stream,
which is the whole point of keeping them separate.

Setup: talk to @BotFather on Telegram, /newbot, and put the token in .env as
TELEGRAM_BOT_TOKEN. The first message you send tells you your chat id; put that in
TELEGRAM_ALLOWED_IDS so nobody else can drive your laptop.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx2

from sommus.brain.loop import Brain, Notice, TextDelta, ToolStarted, TurnDone
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store
from sommus.config import Config

API = "https://api.telegram.org"
MESSAGE_LIMIT = 4000  # Telegram's cap is 4096; leave room for the footer
POLL_SECONDS = 25


class TelegramError(Exception):
    """Setup problem worth stopping for."""


def credentials() -> tuple[str, set[int]]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise TelegramError(
            "No TELEGRAM_BOT_TOKEN in .env. Message @BotFather on Telegram, send /newbot, and paste the token."
        )
    allowed = {int(i) for i in os.environ.get("TELEGRAM_ALLOWED_IDS", "").replace(" ", "").split(",") if i.strip()}
    return token, allowed


def split(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Telegram rejects anything over ~4096 characters; break on newlines where possible."""
    chunks, remaining = [], text.strip()
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks or ["(no reply)"]


class Bot:
    def __init__(self, token: str, allowed: set[int], brain: Brain, store: Store, cfg: Config):
        self.http = httpx2.AsyncClient(base_url=f"{API}/bot{token}", timeout=POLL_SECONDS + 15)
        self.allowed = allowed
        self.brain = brain
        self.store = store
        self.cfg = cfg
        self.offset: int | None = None
        self.seen_strangers: set[int] = set()

    async def close(self) -> None:
        await self.http.aclose()

    async def call(self, method: str, **params: Any) -> Any:
        response = await self.http.post(f"/{method}", json=params)
        payload = response.json()
        if not payload.get("ok"):
            raise TelegramError(f"Telegram rejected {method}: {payload.get('description')}")
        return payload["result"]

    async def whoami(self) -> str:
        return (await self.call("getMe"))["username"]

    async def say(self, chat: int, text: str) -> None:
        for chunk in split(text):
            await self.call("sendMessage", chat_id=chat, text=chunk)

    async def poll(self) -> list[dict]:
        try:
            return await self.call("getUpdates", timeout=POLL_SECONDS, offset=self.offset)
        except (httpx2.TimeoutException, httpx2.TransportError):
            return []  # phone signal, wifi drop, Telegram hiccup — just poll again

    async def handle(self, chat: int, text: str, log) -> None:
        if text == "/new":
            self.brain.reset()
            await self.say(chat, "Fresh conversation.")
            return
        if text == "/cost":
            count, spent = self.store.cost_today()
            await self.say(chat, f"Today: {count} commands, ${spent:.4f}")
            return

        await self.call("sendChatAction", chat_id=chat, action="typing")
        async with self.brain.lock:
            await self._turn(chat, text, log)

    async def _turn(self, chat: int, text: str, log) -> None:
        reply, tools, notices, cost = [], [], [], 0.0

        async def allow(name: str, args: dict) -> bool:
            return True  # full permission, same as the terminal

        async for event in self.brain.handle(text, allow):
            match event:
                case TextDelta(text=delta):
                    reply.append(delta)
                case ToolStarted(name=name):
                    tools.append(name)
                    await self.call("sendChatAction", chat_id=chat, action="typing")
                case Notice(text=notice):
                    notices.append(notice)
                case TurnDone(cost_usd=spent):
                    cost = spent

        body = "".join(reply).strip() or "(nothing to say)"
        if notices:
            body += "\n\n" + "\n".join(f"! {n}" for n in notices)
        if tools:
            body += f"\n\n· {', '.join(tools)} · ${cost:.4f}"
        await self.say(chat, body)
        log(f"{chat}: {text[:60]} → {', '.join(tools) or 'no tools'} (${cost:.4f})")

    async def run(self, log) -> None:
        while True:
            for update in await self.poll():
                self.offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if not message or "text" not in message:
                    continue
                chat = message["chat"]["id"]
                if chat not in self.allowed:
                    if chat not in self.seen_strangers:
                        self.seen_strangers.add(chat)
                        name = message["from"].get("username") or message["from"].get("first_name", "?")
                        log(
                            f"Ignored a message from chat {chat} (@{name}). "
                            f"If that's you, add TELEGRAM_ALLOWED_IDS={chat} to .env and restart."
                        )
                    continue
                try:
                    await self.handle(chat, message["text"].strip(), log)
                except Exception as e:  # one bad turn must not kill the bot
                    log(f"Error on '{message['text'][:40]}': {type(e).__name__}: {e}")
                    await self.say(chat, f"That went wrong: {type(e).__name__}: {e}")


async def serve(cfg: Config, log) -> None:
    token, allowed = credentials()
    store = Store(cfg.data_dir / "sommus.db")
    async with NodeHub(cfg.nodes, Policy(cfg.overrides)) as hub:
        bot = Bot(token, allowed, Brain(cfg, hub, store), store, cfg)
        try:
            username = await bot.whoami()
            log(f"@{username} is listening" + (f" to {len(allowed)} chat(s)." if allowed else " — no allowed ids yet."))
            if not allowed:
                log("Message the bot once; it will print the chat id to put in TELEGRAM_ALLOWED_IDS.")
            for name, why in hub.unreachable.items():
                log(f"Node '{name}' unreachable: {why}")
            await bot.run(log)
        except asyncio.CancelledError:
            pass
        finally:
            await bot.close()
