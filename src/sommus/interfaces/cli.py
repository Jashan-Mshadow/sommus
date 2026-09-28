"""Terminal interface: `sommus` chat · `check` setup · `tool` one tool · `eval` score the commands."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime
from typing import Any

import anthropic
import numpy as np
from dotenv import load_dotenv
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markup import escape

from sommus import config, evals
from sommus.brain import pin
from sommus.brain.loop import Brain, Notice, TextDelta, ToolFinished, ToolStarted, TurnDone
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy, Tier
from sommus.brain.store import Store

console = Console(highlight=False)


SLOW_TOOLS = {"ask_claude", "web_search"}


class PrivateHistory(FileHistory):
    """Up-arrow history, minus PINs: they'd otherwise sit in data/history in plain text."""

    def store_string(self, string: str) -> None:
        super().store_string(pin.redact(string))


TIER_STYLE = {
    Tier.READ: "green",
    Tier.REVERSIBLE: "cyan",
    Tier.DESTRUCTIVE: "yellow",
    Tier.ALWAYS_ASK: "magenta",
    Tier.BLOCKED: "red",
}

HELP = """[bold]/tools[/]  list tools and their permission tier
[bold]/candidates[/]  basic commands still costing money — the next fast-path additions
[bold]/cost[/]   today's commands and spend
[bold]/new[/]    start a fresh conversation
[bold]/lock[/]   lock personal actions again now (say or type the PIN to unlock)
[bold]/quit[/]   exit (or Ctrl+D)
Ctrl+C during a reply cancels it."""


def _format_args(args: dict[str, Any]) -> str:
    text = ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items())
    return text if len(text) <= 80 else text[:77] + "..."


class TurnView:
    """Renders one turn's events and asks for confirmations."""

    def __init__(self, cfg: config.Config, session: PromptSession, speaker=None):
        self.cfg = cfg
        self.session = session
        self.speaker = speaker  # voice mode: replies are also spoken as they stream
        self.status = console.status(f"[dim]{cfg.name} is thinking…[/]", spinner="dots")
        self.mid_line = False
        self.said: list[str] = []  # the reply so far, to tell its echo from something new
        self.acted = False  # a tool that changes something has run: redoing the turn would do it twice
        self._tier: Tier | None = None

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
        if brain.lock.locked():
            console.print("[dim]  waiting for a Telegram command to finish…[/]")
        await brain.lock.acquire()
        self.status.start()
        said_wait = False
        try:
            async for event in brain.handle(text, self.confirm):
                match event:
                    case TextDelta(text=delta):
                        self.said.append(delta)
                        self.status.stop()
                        if not self.mid_line:
                            console.print(f"[bold magenta]{self.cfg.name.lower()} ›[/] ", end="")
                            self.mid_line = True
                        console.print(delta, end="", markup=False, soft_wrap=True)
                        if self.speaker:
                            self.speaker.feed(delta)
                    case ToolStarted(name=name, input=args, tier=tier):
                        self._tier = tier
                        self._break_line()
                        if self.speaker and name in SLOW_TOOLS and not said_wait and not brain.gate.needs_pin(name):
                            self.speaker.feed("One moment. ")  # these take 10–30 s; silence feels broken
                            said_wait = True
                        style = TIER_STYLE.get(tier, "red")
                        console.print(f"  [dim]→[/] [{style}]{name}[/][dim]({escape(_format_args(args))})[/]")
                    case ToolFinished(output=output, is_error=is_error, decision=decision):
                        if decision == "ran" and self._tier not in (Tier.READ, None):
                            self.acted = True
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
            if self.speaker:
                self.speaker.flush()
            brain.lock.release()


async def chat() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[red]No API key.[/] Copy .env.example to .env, paste your key, then run [bold]sommus check[/].")
        return
    cfg = config.load()
    store = Store(cfg.data_dir / "sommus.db")
    session: PromptSession = PromptSession(history=PrivateHistory(str(cfg.data_dir / "history")))

    bot_task = None
    with console.status("[dim]Starting nodes…[/]"):
        hub = await NodeHub(cfg.nodes, Policy(cfg.overrides)).__aenter__()
    try:
        brain = Brain(cfg, hub, store)
        for name, why in hub.unreachable.items():
            console.print(f"[yellow]! Node '{name}' is unreachable — its tools are unavailable. {escape(why)}[/]")
        count, spent = store.cost_today()
        bot_task, telegram_note = await _start_telegram(cfg, brain, store)
        console.print(
            f"[bold magenta]{cfg.name}[/] [dim]· {cfg.model} · {len(hub.api_tools())} tools · "
            f"today {count} commands, ${spent:.2f}{telegram_note} · /help[/]"
        )
        loop = asyncio.get_running_loop()

        while True:
            try:
                with patch_stdout(raw=True):
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
        if bot_task:
            bot_task.cancel()
        await hub.__aexit__(None, None, None)


MIN_MIC_INPUT = 85  # macOS input volume; Jashan's Mac was at 27, which only heard him up close


def raise_mic_input() -> int | None:
    """Turn the microphone up if macOS has it low. Returns the old level when it changed one."""
    try:
        now = subprocess.run(
            ["osascript", "-e", "input volume of (get volume settings)"], capture_output=True, text=True, timeout=5
        )
        level = int(now.stdout.strip())
    except (ValueError, OSError, subprocess.SubprocessError):
        return None
    if level >= MIN_MIC_INPUT - 10 or level < 0:
        return None
    subprocess.run(["osascript", "-e", f"set volume input volume {MIN_MIC_INPUT}"], capture_output=True, timeout=5)
    return level


async def voice_chat() -> None:
    """Talk to Sommus hands-free: say "Hey Sommus", then just talk until the conversation ends.
    Typing works too, and Return wakes it without the wake phrase."""
    from sommus.interfaces import addressee, duplex, voice
    from sommus.interfaces.turn import SmartTurn

    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[red]No API key.[/] See [bold]sommus check[/].")
        return
    cfg = config.load()
    settings = config.section("voice")
    store = Store(cfg.data_dir / "sommus.db")
    session: PromptSession = PromptSession(history=PrivateHistory(str(cfg.data_dir / "history")))
    transcriber = voice.Transcriber(settings.get("stt_model", "mlx-community/whisper-small.en-mlx"))
    smart_turn = SmartTurn() if settings.get("smart_turn", True) else None
    detector = voice.SpeechDetector(
        silence_seconds=float(settings.get("silence_seconds", 0.7)),
        threshold=float(settings.get("vad_threshold", 0.5)),
        endpoint=smart_turn.complete if smart_turn else None,
        check_seconds=float(settings.get("turn_check_seconds", 0.4)),
        patience_seconds=float(settings.get("turn_patience_seconds", 1.6)),
    )
    heard_log = voice.VoiceLog(cfg.data_dir / "voice.log")
    wake = voice.wake_pattern(settings.get("wake_phrases", ["sommus", "hey sommus"]))
    awake_seconds = float(settings.get("awake_seconds", 30))
    # A reply that ends in a question is an invitation to answer: don't make him say the name again.
    awake_after_question = float(settings.get("awake_after_question", 90))

    bot_task = None
    with console.status("[dim]Starting nodes and loading the voice and speech recognition…[/]"):
        hub = await NodeHub(cfg.nodes, Policy(cfg.overrides)).__aenter__()
        engine = await _warm_voice(voice, settings)
        await asyncio.to_thread(transcriber.warm_up)
        await asyncio.to_thread(detector.probability, np.zeros(voice.CHUNK, dtype=np.float32))  # load the VAD
        if smart_turn:
            try:
                await asyncio.to_thread(smart_turn.warm_up)
            except Exception as e:  # not downloaded and offline: a fixed pause ends what he says instead
                console.print(f"[yellow]! Smart Turn unavailable ({escape(str(e))}) — using a fixed pause.[/]")
                detector.endpoint = None
    speaker = voice.Speaker(
        engine,
        on_error=lambda e: console.print(f"[red]! Voice error: {escape(str(e))}[/]"),
        say_as=settings.get("say_as", {}),
    )
    # Barge-in: talk over Sommus to stop it. Needs the echo-cancelling engine and a Kokoro voice
    # (macOS `say` plays outside the engine, so its echo can't be removed).
    engine_io = None
    if settings.get("barge_in", False) and isinstance(engine, voice.KokoroVoice):
        engine_io = duplex.DuplexAudio(gain=float(settings.get("barge_in_mic_gain", 2.0)))
        try:
            await asyncio.to_thread(engine_io.start)
            engine.output = engine_io
        except Exception as e:
            console.print(f"[yellow]! Barge-in unavailable ({escape(str(e))}) — Sommus can't be talked over.[/]")
            engine_io = None
    # With barge_in_needs_name, Sommus listens while it talks but only stops for its own name, so
    # a roommate's voice — or its own, coming back through the echo canceller — can't cut it off.
    needs_name = bool(settings.get("barge_in_needs_name", True))
    names_only = not (engine_io is not None and not needs_name)
    loop = asyncio.get_running_loop()
    heard: asyncio.Queue[tuple[np.ndarray, float, float | None]] = asyncio.Queue()
    state: dict[str, Any] = {"awake_until": 0.0, "deaf_until": 0.0, "busy": False, "held": None, "turn": None}
    hold_seconds = float(settings.get("hold_seconds", 2.0))

    def deaf() -> bool:  # runs on the listener thread; plain reads only
        now = time.monotonic()
        if engine_io is not None:  # echo-cancelled: keep listening while Sommus talks
            return now < state["deaf_until"]
        # Only just after the last word: he often answers the moment Sommus stops, and a closed
        # mic swallows the first syllables (a PIN came through as "day four").
        return state["busy"] or speaker.speaking or now < state["deaf_until"] or now < speaker.quiet_since + 0.2

    def chime(path: str) -> None:
        state["deaf_until"] = time.monotonic() + 0.6  # don't hear our own chime
        voice.chime(path)

    def wake_up() -> None:
        if time.monotonic() >= state["awake_until"]:
            chime(voice.CHIME_WAKE)
        state["awake_until"] = time.monotonic() + awake_seconds

    def fall_asleep() -> None:
        state["awake_until"] = 0.0
        chime(voice.CHIME_SLEEP)
        console.print("[dim]  … sleeping. Say “Hey Sommus” to wake me.[/]")

    def working() -> bool:
        turn = state["turn"]
        return turn is not None and not turn.done()

    def barge_in() -> None:  # cut off whatever Sommus is saying or doing
        work = state.get("work")
        if speaker.speaking or (work is not None and not work.done()):
            speaker.interrupt()
            if work is not None and not work.done():
                work.cancel()
            console.print("[dim]  (interrupted)[/]")

    listener = voice.Listener(
        detector,
        source=(lambda: (duplex.DuplexStream(engine_io), voice.SAMPLE_RATE)) if engine_io else None,
        on_speech=(lambda: loop.call_soon_threadsafe(barge_in)) if engine_io and not needs_name else None,
        # Read on the listener thread the moment the utterance ends, so the scores belong to this one.
        deliver=lambda audio: loop.call_soon_threadsafe(
            heard.put_nowait, (audio, detector.last_peak, detector.last_turn_score)
        ),
        deaf=deaf,
        device=settings.get("input_device"),
        on_error=lambda e: loop.call_soon_threadsafe(
            console.print, f"[red]! Microphone unavailable, still trying: {escape(str(e))}[/]"
        ),
    )

    async def run_turn(text: str, heard_in: float | None) -> None:
        """One request, run beside the microphone: the main loop keeps hearing, so "Sommus, stop" works."""
        state["busy"] = True
        speaker.first_word_at = None
        asked_at = time.monotonic()
        was_awake = time.monotonic() < state["awake_until"]
        view = TurnView(cfg, session, speaker)
        state["view"] = view
        state["asked"] = (text, time.monotonic()) if heard_in is not None else None
        work = asyncio.create_task(view.run(brain, text))
        state["work"] = work
        loop.add_signal_handler(signal.SIGINT, work.cancel)
        try:
            await work
        except asyncio.CancelledError:
            speaker.interrupt()
            console.print("[yellow]! Cancelled.[/]")
        except Exception as e:  # in a background task an error would otherwise vanish, and so would the reply
            speaker.interrupt()
            console.print(f"[red]! That turn failed: {escape(f'{type(e).__name__}: {e}')}[/]")
        finally:
            loop.remove_signal_handler(signal.SIGINT)
        await speaker.finished()
        state["busy"] = False
        if speaker.first_word_at and heard_in is not None:
            console.print(
                f"[dim]  first spoken word {speaker.first_word_at - asked_at + heard_in:.1f}s after you stopped[/]"
            )
        if heard_in is not None or was_awake:  # a typed request doesn't open the mic by itself
            # The follow-up window starts now, and runs longer when Sommus asked him something.
            asked = brain.last_reply().strip().endswith("?")
            state["awake_until"] = time.monotonic() + (awake_after_question if asked else awake_seconds)

    async def start_turn(text: str, heard_in: float | None) -> None:
        nonlocal prompt
        if working():  # a new request replaces the one in progress
            barge_in()
            with contextlib.suppress(asyncio.CancelledError):
                await state["turn"]
        if prompt is not None:  # hand Ctrl+C back to the turn: an open prompt would swallow it
            prompt.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await prompt
            prompt = None
        state["turn"] = asyncio.create_task(run_turn(text, heard_in))

    def follows_on(text: str) -> str | None:
        """The request this was said to finish, if it's the rest of it: "What's my schedule looking like?" …
        "For tomorrow." Said within a few seconds of the request, without the name, and not Sommus's own
        words coming back. These were dropped as room talk, and the first half got a confused answer."""
        asked, view = state.get("asked"), state.get("view")
        if not asked or voice.heard_wake(text, wake) is not None:
            return None
        request, at = asked
        if time.monotonic() - at > FOLLOW_ON_SECONDS:
            return None
        said = "".join(view.said) if view is not None else ""
        # Only while it's still working it out: once it's answering, talk without the name is the room or
        # its own echo, and the name is how to cut in (Jashan's rule).
        if len(said.split()) > HEADS_UP_WORDS or voice.echo_of(text, said):
            return None
        return request

    async def act_on(text: str, seconds: float, heard_in: float, logged: dict[str, Any]) -> None:
        awake = time.monotonic() < state["awake_until"]
        mid_reply = speaker.speaking or working()
        earlier = follows_on(text) if mid_reply else None
        if earlier and settings.get("room_filter", True) and not pin.split_pin(text)[0]:
            context = f"(nothing yet: {cfg.user} just asked “{earlier}”)"
            if not await addressee.said_to_sommus(brain.client, cfg.user, context, text):
                earlier = None
        if earlier:
            view = state.get("view")
            if view is not None and view.acted:  # already did something: run this next, as its own request
                state["next"] = text
                heard_log.write(**logged, outcome="queued: follows the request, runs after it")
                console.print(f"[dim]  (next: “{escape(text)}”)[/]")
                return
            merged = f"{earlier} {text}"
            heard_log.write(**logged, outcome="run: the rest of the request just made")
            console.print(f"[bold]you ›[/] {escape(pin.redact(merged))} [dim](joined)[/]")
            await start_turn(merged, heard_in)
            return
        kind, request = voice.route(text, wake, awake, mid_reply, names_only)
        outcome = kind
        if kind == voice.Route.IGNORE:
            outcome = "ignored: Sommus talking, no name" if mid_reply else "ignored: asleep, no wake phrase"
        elif kind == voice.Route.STOP:
            barge_in()
            state["awake_until"] = max(state["awake_until"], time.monotonic() + awake_seconds)
            console.print("[dim]  stopped. Listening…[/]")
        elif kind == voice.Route.WAKE:
            wake_up()
            console.print("[dim]  listening…[/]")
        elif kind == voice.Route.DISMISS:
            barge_in()
            fall_asleep()
        elif kind == voice.Route.CHECK:
            # Awake and no wake phrase: a follow-up, or talk to someone else in the room? PINs go
            # straight to the brain, never to the check.
            given, _ = pin.split_pin(text)
            if given is None and settings.get("room_filter", True):
                if not await addressee.said_to_sommus(brain.client, cfg.user, brain.last_reply(), text):
                    console.print(f"[dim]  (not for me: “{escape(text)}”)[/]")
                    kind, outcome = voice.Route.IGNORE, "ignored: room filter says not for Sommus"
            if kind == voice.Route.CHECK:
                kind = outcome = voice.Route.RUN
        heard_log.write(**logged, outcome=outcome)
        if kind != voice.Route.RUN:
            return
        if not awake:
            wake_up()
        console.print(
            f"[bold]you ›[/] {escape(pin.redact(request))} "
            f"[dim]({seconds:.1f}s of audio, understood in {heard_in:.2f}s)[/]"
        )
        await start_turn(request, heard_in)

    prompt: asyncio.Task | None = None
    try:
        brain = Brain(cfg, hub, store, voice=True)
        bot_task, telegram_note = await _start_telegram(cfg, brain, store)
        turn_note = " · Smart Turn" if detector.endpoint else ""
        console.print(
            f"[bold magenta]{cfg.name}[/] [dim]· voice · {escape(engine.label)}{turn_note}{telegram_note}[/]\n"
            "[dim]Always listening: say “Hey Sommus”, then just talk. “Sommus, stop” cuts it off. It sleeps again "
            "when the conversation goes quiet. Return wakes it, typing works, /quit exits.[/]"
        )
        if (raised := raise_mic_input()) is not None:
            console.print(f"[dim]Mic input was at {raised}%, turned up to {MIN_MIC_INPUT}% so it hears you "
                          "from across the room.[/]")  # fmt: skip
        speaker.feed(f"{cfg.name} is listening.")
        speaker.flush()
        await speaker.finished()
        listener.start()

        while True:
            busy = working()
            if prompt is None and not busy:
                prompt = asyncio.create_task(_prompt_line(session))
            listening = asyncio.create_task(heard.get())
            waits: set[asyncio.Task] = {listening}
            if prompt is not None:
                waits.add(prompt)
            if busy:
                waits.add(state["turn"])
            now = time.monotonic()
            deadlines = [state["awake_until"]] if now < state["awake_until"] and not busy else []
            if state["held"]:
                deadlines.append(state["held"][1])
            timeout = max(0.0, min(deadlines) - now) if deadlines else None
            done, _ = await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if listening not in done:
                listening.cancel()
            if busy and state["turn"] in done and (following := state.pop("next", None)):
                console.print(f"[bold]you ›[/] {escape(pin.redact(following))} [dim](said while it worked)[/]")
                await start_turn(following, 0.0)
            if not done:
                now = time.monotonic()
                if state["held"] and now >= state["held"][1]:
                    text, _, seconds, logged = state["held"]
                    if detector.buffer:  # still talking: that's the rest of the sentence arriving
                        state["held"] = (text, now + 0.5, seconds, logged)
                    else:  # nothing followed: it was the whole sentence after all
                        state["held"] = None
                        await act_on(text, seconds, 0.0, logged)
                elif state["awake_until"] and now >= state["awake_until"] and not working():
                    if detector.buffer:  # ...unless someone just started talking
                        state["awake_until"] = now + 1
                    else:
                        fall_asleep()
                continue

            if prompt is not None and prompt in done:
                finished, prompt = prompt, None
                try:
                    typed = finished.result()
                except EOFError:
                    break
                if typed == CTRL_C:
                    barge_in()
                elif (typed := typed.strip()).startswith("/"):
                    if not handle_command(typed, brain, hub, store):
                        break
                elif not typed:  # Return: wake without the wake phrase
                    wake_up()
                    console.print("[dim]  listening…[/]")
                else:
                    await start_turn(typed, None)

            if listening not in done:
                continue
            audio, peak, turn_score = listening.result()
            stt_started = time.monotonic()
            waiting_for_pin = bool(brain.gate.pending) and not brain.gate.unlocked
            if waiting_for_pin:  # stay awake while it's waiting: a fumbled PIN shouldn't end the turn
                state["awake_until"] = max(state["awake_until"], time.monotonic() + awake_seconds)
            text = await asyncio.to_thread(transcriber.transcribe, audio, "digits" if waiting_for_pin else None)
            if waiting_for_pin and not re.search(r"\d", text):
                text = await asyncio.to_thread(transcriber.transcribe, audio)  # not a PIN after all
                if not pin.spoken_digits(text, least=1):
                    console.print(f"[dim]  (that wasn't a PIN — heard “{escape(text)}”)[/]")
            heard_in = time.monotonic() - stt_started
            seconds = len(audio) / voice.SAMPLE_RATE
            logged = {
                "seconds": round(seconds, 2),
                "loudness_db": voice.loudness_db(audio),
                "speech_score": round(peak, 2),
                "turn_score": None if turn_score is None else round(turn_score, 2),
                "stt_seconds": round(heard_in, 2),
                "text": text,
            }
            if not text:
                heard_log.write(**logged, outcome="nothing: silence or a Whisper filler")
                continue
            if state["held"]:  # the rest of a sentence that stopped mid-thought
                earlier, _, earlier_seconds, _ = state["held"]
                text, seconds, state["held"] = f"{earlier} {text}", seconds + earlier_seconds, None
                logged["text"] = text
            if voice.sounds_unfinished(text):
                state["held"] = (text, time.monotonic() + hold_seconds, seconds, logged)
                heard_log.write(**logged, outcome="held: sounds unfinished, waiting for the rest")
                if time.monotonic() < state["awake_until"]:  # don't fall asleep while waiting for the rest
                    state["awake_until"] = max(state["awake_until"], state["held"][1] + 1)
                continue
            await act_on(text, seconds, heard_in, logged)
    finally:
        listener.stop()
        if working():
            barge_in()
            with contextlib.suppress(asyncio.CancelledError):
                await state["turn"]
        if engine_io is not None:
            engine_io.stop()
        if bot_task:
            bot_task.cancel()
        await speaker.close()
        await hub.__aexit__(None, None, None)


async def live_chat() -> None:
    """Talk to Sommus through Gemini Live: say "Hey Sommus", then talk like you would to a person — interrupt
    it, change your mind mid-sentence. Sommus keeps the wake word, the tools, the PIN and the rules."""
    from google import genai

    from sommus.interfaces import duplex, live, voice

    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[red]No API key.[/] ask_sommus needs Claude — see [bold]sommus check[/].")
        return
    try:
        client = genai.Client(api_key=live.gemini_key())
    except RuntimeError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        return
    cfg = config.load()
    settings, voice_settings = config.section("live"), config.section("voice")
    store = Store(cfg.data_dir / "sommus.db")
    session: PromptSession = PromptSession(history=PrivateHistory(str(cfg.data_dir / "history")))
    transcriber = voice.Transcriber(voice_settings.get("stt_model", "mlx-community/whisper-small.en-mlx"))
    detector = voice.SpeechDetector(silence_seconds=0.6, threshold=float(voice_settings.get("vad_threshold", 0.5)))
    wake = voice.wake_pattern(voice_settings.get("wake_phrases", ["sommus", "hey sommus"]))
    heard_log = voice.VoiceLog(cfg.data_dir / "voice.log")
    model = settings.get("model", live.MODEL)
    voice_name = settings.get("voice", "Leda")
    awake_seconds = float(settings.get("awake_seconds", 30))
    sensitivity = settings.get("interrupt_sensitivity", "low")

    bot_task = None
    engine_io = duplex.DuplexAudio(gain=float(voice_settings.get("barge_in_mic_gain", 2.0)))
    with console.status("[dim]Starting nodes, the microphone and the wake word…[/]"):
        hub = await NodeHub(cfg.nodes, Policy(cfg.overrides)).__aenter__()
        await asyncio.to_thread(transcriber.warm_up)
        await asyncio.to_thread(detector.probability, np.zeros(voice.CHUNK, dtype=np.float32))
        try:
            await asyncio.to_thread(engine_io.start)
        except Exception as e:
            console.print(f"[red]Live mode needs macOS echo cancellation, which didn't start ({escape(str(e))}).[/]")
            await hub.__aexit__(None, None, None)
            return
    brain = Brain(cfg, hub, store, relay=True)  # Gemini does the talking: Claude hands back plain results
    tools = live.declarations(hub.api_tools())
    system = live.instructions(cfg.name, cfg.user, settings.get("style", ""), settings.get("persona", "butler"))
    loop = asyncio.get_running_loop()
    mic: asyncio.Queue[np.ndarray] = asyncio.Queue()
    reader = live.MicReader(engine_io, lambda chunk: loop.call_soon_threadsafe(mic.put_nowait, chunk))
    state: dict[str, Any] = {
        "mode": "asleep",
        "conversation": None,
        "context": None,
        "receiver": None,
        "pin_until": 0.0,
    }

    def show(line: str) -> None:
        console.print(line)

    def log(**fields: Any) -> None:
        heard_log.write(mode="live", **fields)

    def listen_for_pin() -> None:
        state["mode"], state["pin_until"] = "pin", time.monotonic() + live.PIN_SECONDS
        detector.reset()
        console.print("[dim]  (listening for your PIN here on the Mac — it isn't sent to Google)[/]")

    async def open_conversation(first: str | None = None, handle: str | None = None) -> None:
        voice.chime(voice.CHIME_WAKE)
        try:
            context = client.aio.live.connect(
                model=model, config=live.live_config(system, tools, voice_name, sensitivity, handle)
            )
            live_session = await context.__aenter__()
        except Exception as e:  # offline, quota used up, Google down
            console.print(
                f"[red]! Gemini Live didn't connect ({escape(f'{type(e).__name__}: {e}'[:200])}). "
                "`sommus voice` works without it.[/]"
            )
            state["mode"] = "asleep"
            voice.chime(voice.CHIME_SLEEP)
            return
        conversation = live.LiveConversation(live_session, brain, engine_io, show, log, listen_for_pin)
        conversation.handle = handle
        state.update(mode="awake", conversation=conversation, context=context)
        state["receiver"] = asyncio.create_task(receive(conversation, context))
        if first:
            await conversation.send_text(first)
        console.print("[dim]  live — talk normally; it stops when you talk over it[/]")

    async def receive(conversation, context) -> None:
        try:
            await conversation.receive()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if state["conversation"] is conversation:
                console.print(f"[yellow]! Live connection dropped ({escape(type(e).__name__)}).[/]")
        if state["conversation"] is not conversation:
            return
        handle = conversation.handle if conversation.going_away else None
        await close_conversation(quiet=bool(handle))
        if handle:  # Google closes connections every ~10 minutes: carry on where it left off
            await open_conversation(handle=handle)

    async def close_conversation(quiet: bool = False) -> None:
        conversation, context = state["conversation"], state["context"]
        state.update(mode="asleep", conversation=None, context=None)
        receiver = state.pop("receiver", None)
        if receiver is not None and receiver is not asyncio.current_task():
            receiver.cancel()
        if conversation is not None:
            await conversation.close()
        if context is not None:
            with contextlib.suppress(Exception):
                await context.__aexit__(None, None, None)
        detector.reset()
        if not quiet:
            voice.chime(voice.CHIME_SLEEP)
            console.print("[dim]  … sleeping. Say “Hey Sommus” to wake me.[/]")

    async def heard_asleep(audio: np.ndarray) -> None:
        text = await asyncio.to_thread(transcriber.transcribe, audio)
        if not text:
            return
        request = voice.heard_wake(text, wake)
        log(text=text, outcome="ignored: asleep, no wake phrase" if request is None else "wake")
        if request is not None:
            await open_conversation(first=request or None)

    async def heard_pin(audio: np.ndarray) -> None:
        conversation = state["conversation"]
        text = await asyncio.to_thread(transcriber.transcribe, audio, "digits")
        answer = brain.gate.attempt(text) if text else None
        if answer is None or answer == "":
            return  # not digits, or the first part of a PIN said in pieces
        pending, brain.gate.pending = brain.gate.pending, None
        state["mode"] = "awake"
        log(text="[PIN]", outcome="pin: " + ("unlocked" if brain.gate.unlocked else "refused"))
        if conversation is None:
            return
        if brain.gate.unlocked:
            brain.note_unlocked()
            await conversation.send_text(
                f"[Sommus: his PIN was accepted on the Mac; personal actions are unlocked for "
                f"{cfg.unlock_minutes:g} minutes. Now do what he asked: {pending or 'his last request'}]"
            )
        else:
            await conversation.send_text(f"[Sommus: {answer} Tell him in a few words.]")

    async def pump() -> None:
        """Every mic chunk goes one place: the wake-word check (asleep), Google (live), or the PIN check."""
        batch: list[np.ndarray] = []
        while True:
            chunk = await mic.get()
            mode, conversation = state["mode"], state["conversation"]
            now = time.monotonic()
            if mode == "awake" and conversation is not None:
                batch.append(chunk)
                if len(batch) >= live.SEND_CHUNKS:
                    try:
                        await conversation.send_audio(batch)
                    except Exception:
                        pass  # the receiver notices the dropped connection and reports it
                    batch = []
                quiet_for = now - conversation.last_activity
                if (
                    not engine_io.speaking
                    and not conversation.tasks
                    and (conversation.dismissed or quiet_for > awake_seconds)
                ):
                    await close_conversation()
                continue
            batch = []
            utterance = detector.feed(chunk)
            if mode == "pin":
                if now > state["pin_until"]:
                    state["mode"], brain.gate.pending = "awake", None
                    if conversation is not None:
                        await conversation.send_text("[Sommus: no PIN was given, so it's still locked. Tell him.]")
                elif utterance is not None:
                    await heard_pin(utterance)
            elif utterance is not None:
                await heard_asleep(utterance)

    pumping = None
    try:
        bot_task, telegram_note = await _start_telegram(cfg, brain, store)
        console.print(
            f"[bold magenta]{cfg.name}[/] [dim]· live · Gemini {escape(model)} · voice {escape(voice_name)}"
            f"{telegram_note}[/]\n[dim]Say “Hey Sommus”, then talk normally — interrupt it any time. Nothing goes "
            "to Google before the wake phrase, and PINs never do. Return wakes it, typing works, /quit exits.[/]"
        )
        if (raised := raise_mic_input()) is not None:
            console.print(f"[dim]Mic input was at {raised}%, turned up to {MIN_MIC_INPUT}%.[/]")
        reader.start()
        pumping = asyncio.create_task(pump())
        while True:
            typed = await _prompt_line(session)
            if typed == CTRL_C:
                engine_io.flush()
                continue
            typed = typed.strip()
            if typed.startswith("/"):
                if not handle_command(typed, brain, hub, store):
                    break
                continue
            given, _ = pin.split_pin(typed)
            if typed and given and brain.gate.claims(typed):
                answer = brain.gate.attempt(typed) or ""
                console.print(f"[dim]  {escape(answer)}[/]")
                if brain.gate.unlocked:
                    brain.note_unlocked()
                if brain.gate.unlocked and state["conversation"] is not None:
                    pending, brain.gate.pending = brain.gate.pending, None
                    state["mode"] = "awake"
                    await state["conversation"].send_text(
                        f"[Sommus: PIN accepted; personal actions unlocked. Now do: {pending or 'his last request'}]"
                    )
                continue
            if state["conversation"] is None:
                await open_conversation(first=typed or None)
            elif typed:
                await state["conversation"].send_text(typed)
    except EOFError:
        pass
    finally:
        if pumping is not None:
            pumping.cancel()
        reader.stop()
        if state["conversation"] is not None:
            await close_conversation(quiet=True)
        engine_io.stop()
        if bot_task:
            bot_task.cancel()
        await hub.__aexit__(None, None, None)


async def live_voices(names: list[str]) -> None:
    """Hear Gemini Live's voices, one after another, saying the same line in the configured style."""
    import sounddevice as sd
    from google import genai
    from google.genai import types

    from sommus.interfaces import live

    cfg = config.load()
    settings = config.section("live")
    client = genai.Client(api_key=live.gemini_key())
    line = VOICE_SAMPLE.format(user=cfg.user, name=cfg.name)
    style = settings.get("style", "")
    console.print(
        f"[dim]Current: {escape(settings.get('voice', 'Leda'))} · style: {escape(style or 'none')} · Ctrl+C stops[/]"
    )
    for name in names or list(live.VOICES):
        config_ = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction="Read aloud exactly what you're given, nothing else."
            + (f" Speak with {style}." if style else ""),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=name))
            ),
        )
        audio: list[np.ndarray] = []
        try:
            async with client.aio.live.connect(model=settings.get("model", live.MODEL), config=config_) as s:
                await s.send_realtime_input(text=line)
                async for message in s.receive():
                    content = message.server_content
                    if content and content.model_turn:
                        for part in content.model_turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                audio.append(live.from_pcm16(part.inline_data.data))
                    if content and content.turn_complete:
                        break
        except Exception as e:
            console.print(f"[red]  {escape(name)}: {escape(str(e)[:120])}[/]")
            continue
        console.print(f"[bold]{escape(name)}[/] [dim]— {escape(live.VOICES.get(name, ''))}[/]")
        if audio:
            await asyncio.to_thread(sd.play, np.concatenate(audio), live.OUT_RATE, blocking=True)
    console.print('[dim]Pick one: config.toml → \\[live] voice = "…"; change the accent with style = "…"[/]')


CTRL_C = "\x03"
FOLLOW_ON_SECONDS = 6.0  # said this soon after a request, without the name, it's the rest of that request
HEADS_UP_WORDS = 5  # "On it." / "Checking that now." — more than this and Sommus is already answering


async def _prompt_line(session: PromptSession) -> str:
    """The typing line, run as a task beside the microphone. Ctrl+C comes back as a value: a
    KeyboardInterrupt raised inside a task would escape the event loop and end the session."""
    try:
        with patch_stdout(raw=True):
            return await session.prompt_async(HTML("<b>🎙  </b>"))
    except KeyboardInterrupt:
        return CTRL_C


async def _warm_voice(voice, settings: dict):
    """The configured voice, loaded. Falls back to macOS `say` rather than going silent."""
    try:
        engine = voice.make_voice(settings)
        await asyncio.to_thread(engine.warm_up)
        return engine
    except Exception as e:  # not downloaded and offline, bad voice name, broken install
        console.print(f"[yellow]! Couldn't load the voice ({escape(str(e))}) — using macOS say instead.[/]")
        return voice.SayVoice(settings.get("say_voice", "Samantha"), int(settings.get("say_rate", 190)))


VOICE_SAMPLE = "Hi {user}, I'm {name}. You're at 63%, about five hours left. Want me to lower the brightness?"
# The best-rated English Kokoro voices; `sommus voices am_adam bf_lily` plays any others.
VOICE_SHORTLIST = [
    "af_heart", "af_bella", "af_nicole", "af_sarah", "am_michael", "am_fenrir", "am_puck",
    "bf_emma", "bf_isabella", "bm_george", "bm_fable",
]  # fmt: skip


async def voices(names: list[str]) -> None:
    """Play Kokoro voices one after another, so the voice can be chosen by ear."""
    from threading import Event

    from sommus.interfaces import voice

    cfg = config.load()
    settings = {**config.section("voice"), "engine": "kokoro"}
    engine = voice.make_voice(settings)
    with console.status("[dim]Loading Kokoro (the first time downloads ~330 MB)…[/]"):
        await asyncio.to_thread(engine.warm_up)
    console.print(f"[dim]Current voice: {escape(engine.voice)} · speed {engine.speed} · Ctrl+C to stop[/]")
    sample = VOICE_SAMPLE.format(user=cfg.user, name=cfg.name)
    available = engine.english_voices()
    stop = Event()
    for name in names or VOICE_SHORTLIST:
        console.print(f"[bold]{escape(name)}[/]")
        if name not in available:
            console.print(f"[red]  ! No English voice called {escape(name)}.[/]")
            continue
        engine.voice = name
        audio = await asyncio.to_thread(engine.synthesize, sample)
        await asyncio.to_thread(engine.play, audio, stop)
        await asyncio.to_thread(engine.rest, stop)  # play it out fully before the next voice
    console.print(
        f"[dim]All English voices: {', '.join(available)}\n"
        "Hear any: sommus voices am_adam bf_lily …\n"
        'Pick one: config.toml → \\[voice] kokoro_voice = "…"  (speed = 1.1 talks a little faster)[/]'
    )


def set_pin() -> None:
    """Choose the PIN that unlocks personal actions. Typed hidden; only a salted hash is saved."""
    from getpass import getpass

    cfg = config.load()
    console.print(
        f"[dim]Personal actions ({len(cfg.pin_tools)} tools: email, messages, files, notes, shell, Claude Code) "
        f"stay locked until this PIN is said or typed, then unlock for {cfg.unlock_minutes:g} minutes.[/]"
    )
    first = getpass("New PIN (4–8 digits, hidden): ").strip()
    if getpass("Again: ").strip() != first:
        console.print("[red]Those didn't match — nothing changed.[/]")
        return
    try:
        pin.set_pin(cfg.data_dir, first)
    except ValueError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        return
    console.print("[green]✓[/] PIN saved. Restart any running sommus for it to take effect.")


async def _start_telegram(cfg: config.Config, brain: Brain, store: Store):
    """Serve Telegram from this same session, sharing the brain and its conversation."""
    from sommus import service as background
    from sommus.interfaces import telegram as tg

    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return None, ""
    if background.running_pid(cfg):
        return None, " · Telegram handled by the background service"
    try:
        token, allowed = tg.credentials()
        bot = tg.Bot(token, allowed, brain, store, cfg)
        await bot.whoami()
    except Exception as e:
        console.print(f"[yellow]! Telegram didn't start: {escape(str(e))}[/]")
        return None, ""

    def log(line: str) -> None:
        console.print(f"[dim]  telegram · {escape(line)}[/]")

    async def serve() -> None:
        try:
            await bot.run(log)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # a background task's error is otherwise never shown
            console.print(f"[red]! Telegram stopped: {escape(f'{type(e).__name__}: {e}')} — restart sommus.[/]")
        finally:
            await bot.close()

    return asyncio.create_task(serve()), " · Telegram on"


def show_candidates(store: Store) -> None:
    """Basic commands that still went to the model — add these to brain/fastpath.py."""
    rows = store.fast_path_candidates()
    if not rows:
        console.print("[dim]No candidates yet — every single-tool request is already free.[/]")
        return
    console.print("[bold]Still paying for these basic commands[/] [dim](one tool, no reasoning needed):[/]")
    for tool, text, count, spent in rows:
        console.print(f"  [cyan]{tool:<18}[/] {count}× [dim]${spent:.4f}[/]  {escape(text[:70])}")
    console.print("[dim]Add their phrasings to src/sommus/brain/fastpath.py to make them $0.[/]")


def show_today() -> None:
    """Today's classes, what's next and what's due — no model, $0. Also writes data/today.json,
    which the morning brief reads instead of re-deriving the day from two calendars."""
    from sommus.brain import campus

    schedule = campus.Schedule.load(campus.schedule_path())
    now = datetime.now(schedule.zone)
    out = config.ROOT / "data" / "today.json"
    campus.write_today(schedule, now, out, campus.todo_path())
    console.print(campus.day_summary(schedule, now.date(), now))
    console.print(campus.due_summary(schedule, now))
    console.print(f"[dim]Wrote {out}[/]")


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
    elif command == "/lock":
        brain.gate.lock()
        console.print("[dim]Personal actions locked.[/]")
    elif command == "/candidates":
        show_candidates(store)
    elif command == "/cost":
        console.print(f"[dim]{store.cost_summary(brain.budget.monthly_usd)}[/]")
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
        console.print("[red]No API key.[/] The eval calls the model — see [bold]sommus check[/].")
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


def _chrome_js_check() -> str:
    from sommus.nodes.laptop import browser

    web_tabs = [tab for tab in browser.list_tabs() if tab.url.startswith(("http://", "https://"))]
    if not web_tabs:
        raise RuntimeError("open any website in Chrome, then run this again")
    # Sleeping (Memory Saver) tabs never answer, so one live tab is enough to prove the setting is on.
    for tab in web_tabs:
        try:
            browser._script(f'execute tab {tab.index} of window {tab.window} javascript "1+1"', timeout=4)
            return "on"
        except Exception as e:
            if "turned off" in str(e):
                raise RuntimeError("off — in Chrome: View → Developer → Allow JavaScript from Apple Events") from e
    raise RuntimeError("no tab answered — open or reload any website in Chrome and retry")


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
        ("Chrome JavaScript (reading pages)", _chrome_js_check),
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
        choices=[
            "chat",
            "check",
            "tool",
            "eval",
            "telegram",
            "permissions",
            "start",
            "stop",
            "status",
            "voice",
            "voices",
            "pin",
            "today",
            "stats",
            "live",
        ],
        default="chat",
    )
    parser.add_argument("tool_name", nargs="?", help="with `tool`: the tool to run · with `voices`: a voice to hear")
    parser.add_argument("tool_args", nargs="*", help="with `tool`: key=value arguments · with `voices`: more voices")
    parser.add_argument("--live", action="store_true", help="with `eval`: really run every tool")
    parser.add_argument("--only", help="with `eval`: only commands containing this text")
    parser.add_argument("--gemini", action="store_true", help="with `voices`: hear Gemini Live's voices instead")
    args = parser.parse_args()
    load_dotenv(config.ROOT / ".env")
    commands = {
        "chat": chat,
        "check": check,
        "telegram": telegram,
        "permissions": permissions,
        "voice": voice_chat,
        "live": live_chat,
    }
    try:
        if args.command == "tool":
            asyncio.run(run_tool(args.tool_name, args.tool_args))
        elif args.command == "eval":
            asyncio.run(run_eval(args.live, args.only))
        elif args.command == "pin":
            set_pin()
        elif args.command == "today":
            show_today()
        elif args.command == "stats":
            from sommus import stats

            out = stats.write(
                config.ROOT / "data" / "sommus.db",
                config.ROOT / "data" / "evals",
                config.ROOT / "docs" / "MEASUREMENTS.md",
            )
            console.print(f"Wrote {out.relative_to(config.ROOT)}")
        elif args.command == "voices":
            names = [n for n in [args.tool_name, *args.tool_args] if n]
            asyncio.run(live_voices(names) if args.gemini else voices(names))
        elif args.command in ("start", "stop", "status"):
            asyncio.run(service(args.command))
        else:
            asyncio.run(commands[args.command]())
    except KeyboardInterrupt:
        pass
