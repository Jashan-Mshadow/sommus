"""Scenes: one phrase, several devices, plain code, $0.

"Goodnight" turns the lamp off, quiets the Mac and puts the phone in Sleep Focus; "I'm leaving" switches the lamp
off and locks the Mac. Each scene is a list of steps in config.toml's [scenes], each step a tool and its
arguments. Steps on different devices run at the same time, steps on one device in the order written (lock the
Mac last). One device being away never fails the scene: the reply says what happened per device ("Lamp off, Mac
locked. Phone: skipped, it only acts from the Action Button.").

A scene gets no extra rights. Every step runs through the brain's own tool runner (tiers, the outside-content
rule, the plug's Bluetooth fallback, "locking the Mac locks the PIN"), and a step that would need the PIN or a
yes/no is skipped and named instead of asking halfway through: running the scene again after the PIN does it.
The Mac's steps go through the hub like any other call, so a Mac that's asleep answers "couldn't reach it" (or,
for the few [mac_queue] tools, waits for it to wake, brain/later.py).

The config is read on every use, so an edited scene works without a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sommus import devices
from sommus.brain import fastpath

# Words around a scene's name that don't change which scene is meant: "run my goodnight scene", "start study mode".
FILLER = {
    "please", "pls", "sommus", "somis", "sommis", "hey", "yo", "ok", "okay", "can", "could", "would", "you", "run",
    "start", "activate", "trigger", "begin", "my", "the", "scene", "routine", "mode", "now", "go", "into", "enter",
    "put", "turn", "on", "switch", "to", "me", "in", "thanks", "thank",
}  # fmt: skip
LIST_WORDS = {
    "what", "whats", "which", "list", "show", "my", "do", "i", "have", "are", "there", "the", "all", "available",
    "sommus", "hey", "please", "tell", "me", "about", "can", "you", "run", "got", "scenes", "scene", "routines",
}  # fmt: skip
NOT_IN_A_SCENE = {"run_scene", "list_scenes"}  # a scene inside a scene could loop forever
ASLEEP = "isn't reachable right now"


@dataclass(frozen=True)
class Step:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    device: str = ""  # "mac" | "phone" | "home" | "server"; "" = the device that owns the tool
    done: str = ""  # said when it worked: "lamp off"
    label: str = ""  # who to blame when it didn't: "Lamp"
    fallback: str = ""  # tried when the tool fails: the plug's Bluetooth (ble_plug_off) after the cloud
    fallback_args: dict[str, Any] = field(default_factory=dict)
    options: tuple[Step, ...] = ()  # "any": the first whose device is available, his own device first

    @property
    def said(self) -> str:
        return self.done or self.tool.replace("_", " ") + " done"


@dataclass(frozen=True)
class Scene:
    key: str
    name: str
    phrases: tuple[str, ...]
    steps: tuple[Step, ...]
    say: str = ""

    def describe(self) -> str:
        return f"{self.name} ({', '.join(s.said for s in self.steps) or 'no steps'})"


def _step(raw: Any) -> Step | None:
    if not isinstance(raw, dict):
        return None
    options = tuple(s for s in (_step(o) for o in raw.get("any", []) or []) if s)
    tool = str(raw.get("tool", "")).strip()
    if not tool and options:
        tool = options[0].tool
    if not tool or tool in NOT_IN_A_SCENE or any(o.tool in NOT_IN_A_SCENE for o in options):
        return None
    return Step(
        tool=tool,
        args=dict(raw.get("args", {}) or {}),
        device=str(raw.get("device", "")).strip().lower(),
        done=str(raw.get("done", "")).strip(),
        label=str(raw.get("label", "")).strip(),
        fallback=str(raw.get("fallback", "")).strip(),
        fallback_args=dict(raw.get("fallback_args", {}) or {}),
        options=options,
    )


def load(section: dict) -> dict[str, Scene]:
    """[scenes] from config.toml: {key: Scene}. Steps it can't use are left out, never guessed at."""
    book = {}
    for key, raw in (section or {}).items():
        if not isinstance(raw, dict):
            continue
        steps = tuple(s for s in (_step(r) for r in raw.get("steps", []) or []) if s)
        name = str(raw.get("name", "") or key).strip()
        phrases = tuple(str(p) for p in raw.get("phrases", []) or [] if str(p).strip())
        book[key] = Scene(key, name, phrases, steps, str(raw.get("say", "")).strip())
    return book


def read_config() -> dict[str, Scene]:
    from sommus import config

    with contextlib.suppress(Exception):
        return load(config.section("scenes"))
    return {}


def _core(text: str) -> str:
    """'Run my goodnight scene' and 'good night' both → 'goodnight'; 'I'm leaving now' → 'imleaving'."""
    return "".join(t for t in fastpath.words(text) if t not in FILLER)


def find(text: str, book: dict[str, Scene]) -> Scene | None:
    """The scene this whole message names, or None. Every word must be the scene's name or filler: "goodnight,
    what's the weather tomorrow" or "I'm leaving at 5" aren't scene requests."""
    said = _core(text)
    if not said:
        return None
    for scene in book.values():
        for phrase in (scene.key, scene.name, *scene.phrases):
            if _core(phrase) == said:
                return scene
    return None


def asks_list(text: str) -> bool:
    """'what scenes do I have', 'list my scenes'."""
    tokens = fastpath.words(text)
    return bool({"scenes", "scene", "routines"} & set(tokens)) and all(t in LIST_WORDS for t in tokens)


def fast_match(text: str, book: dict[str, Scene] | None = None) -> fastpath.Match | None:
    """The fast path's scene intents: run one, or list them."""
    book = read_config() if book is None else book
    if not book:
        return None
    if scene := find(text, book):
        return fastpath.Match("scene", "run_scene", {"name": scene.key}, phrase=lambda result: result)
    if asks_list(text):
        return fastpath.Match("scenes", "list_scenes", phrase=lambda result: result)
    return None


def say_list(book: dict[str, Scene]) -> str:
    if not book:
        return "You don't have any scenes yet. They go in config.toml under [scenes]."
    shown = "; ".join(s.describe() for s in book.values())
    return f"You have {len(book)} scene{'s' * (len(book) != 1)}: {shown}."


@dataclass
class Outcome:
    ok: bool
    said: str
    down: bool = False  # the device couldn't be reached: its other steps are skipped


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def report(outcomes: list[Outcome], say: str = "") -> str:
    done = [o.said for o in outcomes if o.ok]
    problems = list(dict.fromkeys(_sentence(o.said) for o in outcomes if not o.ok))
    parts = []
    if done:
        joined = ", ".join(done)
        parts.append(_sentence(joined[0].upper() + joined[1:]))
    parts += problems
    if say:
        parts.append(_sentence(say))
    return " ".join(parts) or "That scene has nothing in it."


Call = Callable[[str, dict[str, Any]], Awaitable[Any]]


class Scenes:
    """Runs scenes. The server fills in the hooks: `call` runs one tool through the brain's own runner and
    returns its ToolResult; `available(device)`, `here()` (the device he's talking from), `device_of(tool)`,
    `has_tool(tool)` and `needs_pin(tool)` answer from the hub, the phone and the PIN gate."""

    def __init__(self, read: Callable[[], dict[str, Scene]] = read_config):
        self.read = read
        self.call: Call | None = None
        self.available: Callable[[str], bool] = lambda device: device != "phone"
        self.here: Callable[[], str] = lambda: ""
        self.device_of: Callable[[str], str] = lambda tool: "server"
        self.has_tool: Callable[[str], bool] = lambda tool: True
        self.needs_pin: Callable[[str], bool] = lambda tool: False

    def node(self):
        from mcp.server.mcpserver import MCPServer
        from mcp.types import ToolAnnotations

        server = MCPServer("scenes", log_level="WARNING")
        names = ", ".join(s.name for s in self.read().values()) or "none yet"

        described = (
            "Run one of Jashan's scenes: one phrase that does several things across his devices at once (the lamp, "
            f"the Mac, the phone). His scenes: {names}. Each step keeps its own permission rules; the result says "
            "what happened on each device: tell him that, never more. Use it when he names a scene or says its "
            'phrase ("goodnight"). `name`: the scene\'s name or one of its phrases.'
        )

        @server.tool(
            description=described,
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
            structured_output=False,
        )
        async def run_scene(name: str) -> str:
            return await self.run(name)

        @server.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
        def list_scenes() -> str:
            """List Jashan's scenes and what each one does."""
            return say_list(self.read())

        return server

    async def run(self, name: str) -> str:
        book = self.read()
        scene = book.get(name) or find(name, book)
        if scene is None:
            known = ", ".join(s.name for s in book.values()) or "none yet"
            return f"There's no scene called {name}. Scenes: {known}."
        if self.call is None:
            return "Scenes can't run here: no device runner is connected."
        steps = [self._choose(step) for step in scene.steps]
        groups: dict[str, list[int]] = {}
        for index, step in enumerate(steps):
            groups.setdefault(self._device(step), []).append(index)
        outcomes: list[Outcome | None] = [None] * len(steps)

        async def run_group(device: str, indexes: list[int]) -> None:
            down: Outcome | None = None
            for index in indexes:
                if down is not None:
                    outcomes[index] = Outcome(False, down.said)  # said once: report() drops repeats
                    continue
                outcome = await self._run_step(steps[index], device)
                outcomes[index] = outcome
                if outcome.down:
                    down = outcome

        await asyncio.gather(*(run_group(device, indexes) for device, indexes in groups.items()))
        return report([o for o in outcomes if o], scene.say)

    def _device(self, step: Step) -> str:
        return step.device or self.device_of(step.tool)

    def _choose(self, step: Step) -> Step:
        """An "any" step: the option whose device is available, the device he's talking from first."""
        if not step.options:
            return step
        for device in devices.order(self.here()):
            for option in step.options:
                if self._device(option) == device and self.available(device) and self.has_tool(option.tool):
                    return dataclasses.replace(option, done=option.done or step.done, label=option.label or step.label)
        first = step.options[0]
        return dataclasses.replace(first, done=first.done or step.done, label=first.label or step.label)

    async def _run_step(self, step: Step, device: str) -> Outcome:
        label = step.label or devices.LABELS.get(device, device.capitalize())
        tool_said = step.tool.replace("_", " ")
        if device == "phone" and not self.available("phone"):
            return Outcome(False, "Phone: skipped, it only acts while you're using the Action Button", down=True)
        if not self.has_tool(step.tool):
            return Outcome(False, f"{label}: {tool_said} isn't available here")
        if self.needs_pin(step.tool):
            return Outcome(False, f"Skipped {tool_said}: it needs your PIN (say it, then run the scene again)")
        assert self.call is not None
        result = await self.call(step.tool, dict(step.args))
        if result.is_error and step.fallback and self.has_tool(step.fallback):
            result = await self.call(step.fallback, dict(step.fallback_args))
        decision = getattr(result, "decision", "ran")
        text = str(getattr(result, "text", ""))
        if decision == "needs_pin":
            return Outcome(False, f"Skipped {tool_said}: it needs your PIN (say it, then run the scene again)")
        if decision == "declined":
            return Outcome(False, f"Skipped {tool_said}: it asks first, so run it on its own")
        if decision == "blocked":
            return Outcome(False, f"Skipped {tool_said}: blocked")
        if result.is_error:
            if ASLEEP in text:
                return Outcome(False, f"{label}: couldn't reach it", down=True)
            reason = fastpath_error(text)
            return Outcome(False, f"{label}: {reason}")
        if text.startswith("Not done"):  # the phone's own refusals (server.Phone.queue)
            if "one thing per request" in text:
                said = f"{label}: your Shortcut takes one action per request, so {step.said} was skipped"
                return Outcome(False, said)
            return Outcome(False, f"{label}: skipped, it only acts while you're using the Action Button", down=True)
        if "is queued" in text:  # brain/later.py: it runs when the Mac wakes
            return Outcome(True, f"{step.said} once the Mac wakes")
        return Outcome(True, step.said)


def fastpath_error(text: str) -> str:
    from sommus.brain.nodes import plain_error

    said = plain_error(text).strip().rstrip(".")
    return said if len(said) <= 90 else said[:87] + "..."
