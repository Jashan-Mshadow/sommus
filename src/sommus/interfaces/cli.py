"""Terminal interface: `sommus` chat · `check` setup · `tool` one tool · `eval` score the commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
from datetime import datetime
from typing import Any

import anthropic
from dotenv import load_dotenv
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.markup import escape

from sommus import config, evals
from sommus.brain.loop import Brain, Notice, TextDelta, ToolFinished, ToolStarted, TurnDone
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy, Tier
from sommus.brain.store import Store

console = Console(highlight=False)

TIER_STYLE = {
    Tier.READ: "green",
    Tier.REVERSIBLE: "cyan",
    Tier.DESTRUCTIVE: "yellow",
    Tier.ALWAYS_ASK: "magenta",
    Tier.BLOCKED: "red",
}

HELP = """[bold]/tools[/]  list tools and their permission tier
[bold]/cost[/]   today's commands and spend
[bold]/new[/]    start a fresh conversation
[bold]/quit[/]   exit (or Ctrl+D)
Ctrl+C during a reply cancels it."""


def _format_args(args: dict[str, Any]) -> str:
    text = ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items())
    return text if len(text) <= 80 else text[:77] + "..."


class TurnView:
    """Renders one turn's events and asks for confirmations."""

    def __init__(self, cfg: config.Config, session: PromptSession):
        self.cfg = cfg
        self.session = session
        self.status = console.status(f"[dim]{cfg.name} is thinking…[/]", spinner="dots")
        self.mid_line = False

    def _break_line(self) -> None:
        self.status.stop()
        if self.mid_line:
            console.print()
            self.mid_line = False

    async def confirm(self, name: str, args: dict[str, Any]) -> bool:
        self._break_line()
        try:
            answer = await self.session.prompt_async(HTML(f"    <ansiyellow><b>Allow {name}?</b></ansiyellow> [y/N] "))
        except (KeyboardInterrupt, EOFError):
            return False
        return answer.strip().lower() in ("y", "yes")

    async def run(self, brain: Brain, text: str) -> None:
        self.status.start()
        try:
            async for event in brain.handle(text, self.confirm):
                match event:
                    case TextDelta(text=delta):
                        self.status.stop()
                        if not self.mid_line:
                            console.print(f"[bold magenta]{self.cfg.name.lower()} ›[/] ", end="")
                            self.mid_line = True
                        console.print(delta, end="", markup=False, soft_wrap=True)
                    case ToolStarted(name=name, input=args, tier=tier):
                        self._break_line()
                        style = TIER_STYLE.get(tier, "red")
                        console.print(f"  [dim]→[/] [{style}]{name}[/][dim]({escape(_format_args(args))})[/]")
                    case ToolFinished(output=output, is_error=is_error, decision=decision):
                        mark = (
                            "[green]✓[/]" if not is_error else "[yellow]✗[/]" if decision == "declined" else "[red]✗[/]"
                        )
                        console.print(f"    {mark} [dim]{escape(output)}[/]")
                        self.status.start()
                    case Notice(text=notice):
                        self._break_line()
                        console.print(f"[yellow]! {notice}[/]")
                    case TurnDone(usage=usage, cost_usd=cost, steps=steps):
                        self._break_line()
                        tokens = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
                        console.print(
                            f"[dim]  {steps} step{'s' * (steps != 1)} · {tokens:,} in "
                            f"({usage.cache_read_tokens:,} cached) · {usage.output_tokens:,} out · ${cost:.4f}[/]"
                        )
        finally:
            self._break_line()


async def chat() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[red]No API key.[/] Copy .env.example to .env, paste your key, then run [bold]sommus check[/].")
        return
    cfg = config.load()
    store = Store(cfg.data_dir / "sommus.db")
    session: PromptSession = PromptSession(history=FileHistory(str(cfg.data_dir / "history")))

    with console.status("[dim]Starting nodes…[/]"):
        hub = await NodeHub(cfg.nodes, Policy(cfg.overrides)).__aenter__()
    try:
        brain = Brain(cfg, hub, store)
        for name, why in hub.unreachable.items():
            console.print(f"[yellow]! Node '{name}' is unreachable — its tools are unavailable. {escape(why)}[/]")
        count, spent = store.cost_today()
        console.print(
            f"[bold magenta]{cfg.name}[/] [dim]· {cfg.model} · {len(hub.api_tools())} tools · "
            f"today {count} commands, ${spent:.2f} · /help[/]"
        )
        loop = asyncio.get_running_loop()

        while True:
            try:
                text = (await session.prompt_async(HTML("<b>you ›</b> "))).strip()
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            if not text:
                continue
            if text.startswith("/"):
                if not handle_command(text, brain, hub, store):
                    break
                continue

            turn = asyncio.create_task(TurnView(cfg, session).run(brain, text))
            loop.add_signal_handler(signal.SIGINT, turn.cancel)
            try:
                await turn
            except asyncio.CancelledError:
                console.print("[yellow]! Cancelled.[/]")
            finally:
                loop.remove_signal_handler(signal.SIGINT)
    finally:
        await hub.__aexit__(None, None, None)


def handle_command(text: str, brain: Brain, hub: NodeHub, store: Store) -> bool:
    """Returns False to exit."""
    command = text.split()[0].lower()
    if command in ("/quit", "/exit"):
        return False
    if command == "/new":
        brain.reset()
        console.print("[dim]Fresh conversation.[/]")
    elif command == "/tools":
        for t in hub.tools:
            console.print(f"  [{TIER_STYLE[t.tier]}]{t.tier.value:<12}[/] {t.tool.name} [dim]({t.node})[/]")
    elif command == "/cost":
        count, spent = store.cost_today()
        console.print(f"[dim]Today: {count} commands, ${spent:.4f}[/]")
    else:
        console.print(HELP)
    return True


async def check() -> None:
    """Verify every piece of setup, without spending API tokens."""
    ok = True

    def report(passed: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        mark = "[green]✓[/]" if passed else "[red]✗[/]"
        console.print(f"{mark} {label}" + (f" [dim]— {detail}[/]" if detail else ""))

    cfg = config.load()
    report(True, "config.toml", f"{cfg.name}, {cfg.model}, effort {cfg.effort}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        report(False, "API key", "ANTHROPIC_API_KEY is not set — copy .env.example to .env and paste your key")
    else:
        try:
            model = await anthropic.AsyncAnthropic().models.retrieve(cfg.model)
            report(True, "API key", f"works, {model.display_name} available")
        except anthropic.AuthenticationError:
            report(False, "API key", "rejected by the API — create a new key in the Console")
        except anthropic.APIError as e:
            report(False, "API key", f"couldn't verify: {e}")

    try:
        async with NodeHub(cfg.nodes, Policy(cfg.overrides)) as hub:
            tiers = {tier: sum(t.tier is tier for t in hub.tools) for tier in Tier}
            report(
                True, "Nodes", f"{len(hub.tools)} tools — " + ", ".join(f"{n} {t.value}" for t, n in tiers.items() if n)
            )
            for name, why in hub.unreachable.items():
                report(False, f"Node '{name}' unreachable", why)
            for name in ("get_battery", "get_volume"):
                result = await hub.call(name, {})
                report(not result.is_error, name, result.text)
    except Exception as e:
        report(False, "Nodes", f"failed to start: {e}")

    from sommus.nodes.gmail import mail

    if not mail.configured():
        report(False, "Gmail", "set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env — see the README")
    else:
        try:
            with mail.imap():
                pass
            report(True, "Gmail", f"connected as {os.environ['GMAIL_ADDRESS']}")
        except mail.MailError as e:
            report(False, "Gmail", str(e))

    from sommus.nodes.laptop import macos

    report(
        macos.can_post_events(),
        "Accessibility permission",
        "granted" if macos.can_post_events() else "needed for media keys and lock_screen — see README",
    )
    console.print("\n[bold green]Ready.[/] Run [bold]sommus[/]." if ok else "\n[bold red]Fix the ✗ items above.[/]")


def _parse_tool_args(pairs: list[str]) -> dict[str, Any]:
    """`level=20 name=Spotify` → {"level": 20, "name": "Spotify"}. Values are JSON when they parse as JSON."""
    args = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"Arguments look like key=value, got '{pair}'.")
        try:
            args[key] = json.loads(value)
        except json.JSONDecodeError:
            args[key] = value
    return args


async def run_tool(name: str | None, pairs: list[str]) -> None:
    """Call a tool directly, no AI involved. For testing nodes without an API key."""
    cfg = config.load()
    async with NodeHub(cfg.nodes, Policy(cfg.overrides)) as hub:
        if name is None:
            for t in hub.tools:
                params = ", ".join(t.tool.input_schema.get("properties", {}))
                console.print(
                    f"  [{TIER_STYLE[t.tier]}]{t.tool.name}[/]({params}) [dim]— {t.tool.description.splitlines()[0]}[/]"
                )
            console.print("\n[dim]Run one: sommus tool set_volume level=20[/]")
            return
        result = await hub.call(name, _parse_tool_args(pairs))
        console.print(f"{'[red]✗' if result.is_error else '[green]✓'}[/] {escape(result.text)}")


async def run_eval(live: bool, only: str | None) -> None:
    """Score Sommus against evals/commands.toml."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[red]No API key.[/] The eval calls Claude — see [bold]sommus check[/].")
        return
    cfg = config.load()
    cases = [c for c in evals.load_cases() if only is None or only.lower() in c.text.lower()]
    if not cases:
        console.print("[red]No matching commands in evals/commands.toml.[/]")
        return

    console.print(
        f"[bold]{len(cases)} commands[/] · {cfg.model}, effort {cfg.effort} · "
        + ("[red]LIVE — tools really run[/]" if live else "[dim]read tools run, others simulated[/]")
    )
    store = Store(cfg.data_dir / "sommus.db")
    results = []
    async with NodeHub(cfg.nodes, Policy(cfg.overrides)) as real_hub:
        hub = real_hub if live else evals.SimulatingHub(real_hub)
        for i, case in enumerate(cases, 1):
            with console.status(f"[dim]{i}/{len(cases)} {case.text}[/]"):
                result = await evals.run_case(cfg, hub, store, case)
            results.append(result)
            mark = "[green]PASS[/]" if result.passed else "[red]FAIL[/]"
            console.print(f"{mark} [bold]{escape(case.text)}[/]")
            console.print(f"     [dim]called {', '.join(result.called) or '(nothing)'}[/]")
            if result.missing:
                console.print(f"     [red]missing {', '.join(result.missing)}[/]")
            if result.extra:
                console.print(f"     [yellow]extra {', '.join(result.extra)}[/]")
            for notice in result.notices:
                console.print(f"     [yellow]! {escape(notice)}[/]")
            console.print(f"     [dim]{escape(result.reply.strip()[:100])}[/]")
            console.print(f"     [dim]{result.seconds:.1f}s · ${result.cost_usd:.4f}[/]")

    passed = sum(r.passed for r in results)
    cost = sum(r.cost_usd for r in results)
    seconds = sum(r.seconds for r in results) / len(results)
    style = "green" if passed / len(results) >= 0.9 else "yellow" if passed / len(results) >= 0.7 else "red"
    console.print(
        f"\n[bold {style}]{passed}/{len(results)} passed[/] · ${cost:.4f} total, "
        f"${cost / len(results):.4f} per command · {seconds:.1f}s average"
    )
    console.print(f"[dim]Saved to {evals.save(results, live, cfg)}[/]")


async def telegram() -> None:
    """Run Sommus as a Telegram bot until Ctrl+C."""
    from sommus.interfaces import telegram as bot

    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[red]No API key.[/] See [bold]sommus check[/].")
        return
    cfg = config.load()

    def log(line: str) -> None:
        console.print(f"[dim]{datetime.now():%H:%M:%S}[/] {escape(line)}")

    console.print(f"[bold magenta]{cfg.name}[/] [dim]on Telegram · {cfg.model} · Ctrl+C to stop[/]")
    try:
        await bot.serve(cfg, log)
    except bot.TelegramError as e:
        console.print(f"[red]{escape(str(e))}[/]")


async def permissions() -> None:
    """Trigger every macOS permission prompt at once, while you're here to click Allow."""
    from sommus.nodes.laptop import apps, browser, macos

    console.print("[bold]Warming up macOS permissions.[/] Click [bold]OK / Allow[/] on each prompt.\n")
    checks = [
        ("Accessibility (keys, typing, clicking)", lambda: "granted" if macos.can_post_events() else None),
        ("Screen Recording (screenshots)", lambda: f"{macos.screenshot()[1]}px wide"),
        ("Contacts", lambda: f"{len(apps.contacts())} contacts"),
        ("Reminders", lambda: f"{len(apps.list_reminders(3))} open reminders"),
        ("Chrome", lambda: f"{len(browser.list_tabs())} tabs"),
        ("Chrome JavaScript (reading pages)", lambda: browser._js(browser.list_tabs()[0], "document.title") and "on"),
        ("Messages", lambda: macos._osascript('tell application "Messages" to get name', timeout=20) or "ready"),
    ]
    missing = []
    for label, check in checks:
        try:
            result = check()
        except Exception as e:
            result, detail = None, str(e).split(".")[0]
        else:
            detail = ""
        if result:
            console.print(f"[green]✓[/] {label} [dim]— {escape(str(result))}[/]")
        else:
            missing.append(label)
            console.print(f"[red]✗[/] {label} [dim]— {escape(detail or 'not granted')}[/]")
    if missing:
        console.print(
            "\n[yellow]Still missing:[/] " + ", ".join(missing) + "\n[dim]Accessibility and Screen Recording "
            "are granted in System Settings → Privacy & Security (add Terminal, then restart it). "
            "The rest prompt on use — run this again to retry.[/]"
        )
    else:
        console.print("\n[bold green]All set.[/] Sommus won't ask again, including when running in the background.")


async def service(action: str) -> None:
    """Keep the Telegram bot running after the terminal closes."""
    from sommus import service as background

    cfg = config.load()
    if action == "start":
        if not os.environ.get("TELEGRAM_BOT_TOKEN"):
            console.print("[red]No TELEGRAM_BOT_TOKEN in .env.[/]")
            return
        try:
            pid, log = background.start(cfg)
        except RuntimeError as e:
            console.print(f"[red]{escape(str(e))}[/]")
            return
        console.print(f"[green]✓[/] Running in the background [dim](pid {pid}) — log: {log}[/]")
        console.print("[dim]Close this window whenever. Stop it with: sommus-service stop[/]")
    elif action == "stop":
        pid = background.stop(cfg)
        console.print(f"[green]✓[/] Stopped (pid {pid})." if pid else "[dim]Not running.[/]")
    else:
        pid = background.running_pid(cfg)
        console.print(f"[green]●[/] Running (pid {pid})." if pid else "[dim]○ Not running.[/]")
        console.print(f"[dim]{escape(background.tail(cfg, 12))}[/]")


def main() -> None:
    parser = argparse.ArgumentParser(prog="sommus")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["chat", "check", "tool", "eval", "telegram", "permissions", "start", "stop", "status"],
        default="chat",
    )
    parser.add_argument("tool_name", nargs="?", help="with `tool`: the tool to run (omit to list them)")
    parser.add_argument("tool_args", nargs="*", help="with `tool`: key=value arguments")
    parser.add_argument("--live", action="store_true", help="with `eval`: really run every tool")
    parser.add_argument("--only", help="with `eval`: only commands containing this text")
    args = parser.parse_args()
    load_dotenv(config.ROOT / ".env")
    commands = {"chat": chat, "check": check, "telegram": telegram, "permissions": permissions}
    try:
        if args.command == "tool":
            asyncio.run(run_tool(args.tool_name, args.tool_args))
        elif args.command == "eval":
            asyncio.run(run_eval(args.live, args.only))
        elif args.command in ("start", "stop", "status"):
            asyncio.run(service(args.command))
        else:
            asyncio.run(commands[args.command]())
    except KeyboardInterrupt:
        pass
