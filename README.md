# Sommus

[![CI](https://github.com/Jashan-Mshadow/sommus/actions/workflows/ci.yml/badge.svg)](https://github.com/Jashan-Mshadow/sommus/actions/workflows/ci.yml)

A personal assistant that controls my devices. Named after Somnus, the Roman god of sleep.

**Phase 1:** type a command in the terminal, and Sommus controls my MacBook.
Next up: Bluetooth speakers, then voice, then phone and custom hardware.

What a session looks like (illustrative):

```
you › study mode: close messages, open obsidian, volume 10
  → quit_app(name="Messages")
    ✓ Asked Messages to quit.
  → open_app(name="Obsidian")
    ✓ Opened Obsidian.
  → set_volume(level=10)
    ✓ Volume 10%.
sommus › Study mode is on: Messages closed, Obsidian open, volume at 10.
  4 steps · 9,812 in (7,904 cached) · 212 out · $0.0141
```

## Measured, not guessed

| | |
|---|---|
| Everyday commands (volume, apps, music, weather, "where's my next class") | **$0, ~0.1 s**, no model call |
| Commands that need the model | **~0.5–1¢**, 3–5 s (Sonnet 5, low effort) |
| Prompt tokens served from cache | **89%** |
| Class and room questions | **20–27 s → under 1 ms** (schedule engine instead of two calendar lookups) |
| Tool-call accuracy | **45/46** on the eval suite (Sonnet 5); Haiku 4.5 41/46 at 0.26¢ |

Full numbers, regenerated from the log with `sommus stats`: [docs/MEASUREMENTS.md](docs/MEASUREMENTS.md).
Why it's built this way: [docs/decisions](docs/decisions/) — MCP nodes, permission tiers and the PIN,
the model sweep, no local LLM, one model with a budget guard instead of per-request routing, and the
fast path.

## What it can do

**60+ tools across five nodes**, plus live web search:

| Area | Tools |
|---|---|
| Sound | volume, mute, play/pause/skip, what's playing |
| Display | brightness, screen off, lock |
| Apps | list open apps, open, quit, focus, any keyboard shortcut |
| Web & files | open a URL, Spotlight search, read a file or folder, append a line, open a file |
| Clipboard | read, write |
| Reminders | create (syncs to iPhone), list |
| Notes vault | search, read, append, add to the to-do list, commit |
| Email | search, read, send, reply, draft over IMAP/SMTP |
| System | battery, Wi-Fi status and toggle, sleep, notifications |
| Browser | list Chrome tabs, read a tab's text, list its links, click a link or button, switch tabs |
| Seeing & typing | **screenshot** (an image the model looks at), click at its coordinates, type any text, any keystroke, wait |
| Documents | read a PDF as text — scanned pages go through macOS Vision OCR |
| Contacts | look up anyone's number or email from the Contacts app |
| Downloads | fetch a file straight to disk, or save a logged-in page with Cmd+S |
| Messaging | send an iMessage, write or send a Gmail |
| Escape hatch | run any of the user's macOS **Shortcuts** — Focus modes, Home devices, anything macOS won't script |
| Knowledge | **web search** for weather, news, prices, anything after the model's cutoff |

It answers questions as readily as it acts, and when there's no exact tool it tries the nearest route
(a Shortcut, a keystroke, opening the right settings pane) before saying it can't.

### Without a model

- **Fast path** (`brain/fastpath.py`): an intent claims a request only when every word is in its vocabulary —
  "brightness down 12", "screen's too dim", "quit Messages", "what's playing", weather and time anywhere,
  holidays. A miss just goes to the model; a wrong match would do the wrong thing, so it leans towards missing.
- **Campus engine** (`brain/campus.py`): next class, room, what's left today and what's due this week, from a
  schedule file. `sommus today` prints the day and writes `data/today.json` for other tools.
- **Long-term memory** (`brain/memory.py`): "remember my gym days are Monday, Wednesday and Friday" is saved to
  a private note and loaded into the cached prompt. Anything that looks like a password or code is refused.
- **Budget guard** (`brain/budget.py`): a heads-up at 80% of the monthly cap, a cheaper model past 90%.

## Nodes

| Node | Tools | Setup |
|---|---|---|
| **laptop** | 46 — sound, display, apps, browser, screen, shell, PDFs, contacts, files, clipboard, reminders, shortcuts | macOS permissions (below) |
| **vault** | 9 — search, read by section, list, append, add a to-do, commit, remember, forget | none |
| **gmail** | 6 — search, read, send, reply, draft, mark read | app password in `.env` |
| **web** | 1 — search through a cheap worker model, so results never bloat the main context | API key |
| **claude** | 1 — hands a request to Claude Code for Google Calendar, Drive and Notion | Claude Code installed |

A node that isn't set up reports as unreachable; everything else keeps working.

## Sommus on your phone (Telegram)

The brain doesn't change — Telegram is a second interface over the same event stream.

1. Message [@BotFather](https://t.me/botfather) on Telegram, send `/newbot`, pick a name, copy the token.
2. Put it in `.env` as `TELEGRAM_BOT_TOKEN=...` and leave `TELEGRAM_ALLOWED_IDS` empty for now.
3. Start Sommus, message your bot once, and it prints your chat id:

```bash
sommus
```

4. Put that id in `TELEGRAM_ALLOWED_IDS` and restart. Anyone not on that list is ignored and logged —
   without it, whoever finds the bot could drive the laptop.

Plain `sommus` answers the terminal and Telegram together, sharing one conversation — a lock keeps a
phone command and a typed one from interleaving. `/new` starts fresh, `/cost` reports the day's spend.

### Keeping it running

The terminal session stops when its window closes. For Telegram only, detached:

```bash
sommus start     # background, survives closing the window
sommus status    # is it alive, plus the last log lines
sommus stop
```

Not a LaunchAgent on purpose: macOS ties Accessibility and Automation permissions to the *responsible*
app, which for a launchd job is the bare Python binary — every grant would have to be redone, and
prompts would appear with nobody there to click them. Started from Terminal, the process inherits
Terminal's grants for its whole life. After a reboot, run `sommus start` once.

The laptop still has to be awake: closing the lid pauses everything until it's opened.

### Permissions, once

```bash
sommus permissions
```

Triggers every macOS prompt in one go — Accessibility, Screen Recording, Contacts, Reminders, Chrome,
Chrome's JavaScript setting, Messages — while you're at the keyboard to approve them. Worth running
before relying on the background service, since a prompt that appears while you're away silently
blocks whatever it was doing.

## Talking to Sommus (voice)

```bash
sommus voice     # always listening: say "Hey Sommus", then just talk. Return wakes it; typing works.
sommus voices    # hear the voices and pick one
```

Everything audio stays on the Mac, and none of it costs anything:

| Step | How |
|---|---|
| Waking | Silero VAD cuts the mic into utterances (~1% of a CPU core); Whisper reads each one, and only one that starts or ends with "Sommus", "Hey Sommus", "What's up Sommus" or "Yo Sommus" wakes it. Nothing is kept or sent before that |
| Conversation | Awake, everything said is a request — no wake phrase — until 10 s pass after a reply with nobody talking, or "that's all". The mic is deaf while Sommus speaks, so it never answers itself |
| Understanding | Whisper small.en on Apple silicon (`mlx-whisper`), ~0.35 s per command, offline |
| Speaking | **Kokoro-82M** on Apple silicon (`mlx-audio`), ~330 MB, offline. Sentences are voiced as the reply streams, so speech starts about 0.7 s after the first sentence arrives; Ctrl+C cuts it off |

Only the transcribed text reaches the model, so voice works unchanged wherever the brain runs.
Replies play through the Mac's current output — AirPods included, even if they connect after Sommus starts.
Set the voice in `config.toml` (`[voice] kokoro_voice`, `speed`); `engine = "say"` switches to the built-in
macOS voice, which is also the automatic fallback if Kokoro can't load.

## Connecting Gmail

Sommus talks to Gmail over IMAP and SMTP with an **app password**, not the Gmail API.
Gmail's read scopes are "restricted", so a personal OAuth app can't leave Google's Testing
mode without a security assessment — and tokens in Testing expire every 7 days. An app
password never expires and needs no cloud project.

1. Turn on **2-Step Verification** on the Google account (required for app passwords).
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), name it
   "Sommus", and copy the 16-character password.
3. Put both in `.env` (gitignored):

```
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=abcd efgh ijkl mnop
```

`sommus check` verifies the login. Without it, email still works through the browser
(`compose_email`) and the Gmail tools just explain the setup.

## Architecture

```
  INTERFACES                      BRAIN                         NODES
  ┌──────────────┐          ┌──────────────────────┐   MCP    ┌──────────────────┐
  │ terminal     │─ text ─► │ agent loop           │ ───────► │ laptop (macOS)   │
  │ telegram     │ ◄ events │ LLM API              │ ───────► │ vault (notes)    │
  │ voice        │          │ permission tiers     │ ───────► │ gmail            │
  └──────────────┘          │ audit log + cost     │          └──────────────────┘
                            └──────────────────────┘
```

- **Interfaces** only exchange text and events with the brain. Voice is a new interface, not a rewrite.
- **Nodes** are [MCP](https://modelcontextprotocol.io) servers. Each device lists its tools; the brain routes calls.
  A new device is a new node.
- **The agent loop** is hand-written on the Messages API (`src/sommus/brain/loop.py`): streaming,
  adaptive thinking, prompt caching, refusal fallback, and a step limit.

### Permission tiers

Sommus runs with full permission by default: it acts without asking. Every tool still declares a tier
through MCP annotations, and every call is logged with its tier, so the gate can be switched back on
(`[safety] ask_before_destructive = true`) when riskier tools arrive — email, files, voice.

| Tier | Full permission (default) | `ask_before_destructive = true` | Examples |
|---|---|---|---|
| read | runs | runs | `get_battery`, `list_apps`, `read_file` |
| reversible | runs | runs | `set_volume`, `open_app`, `press_keys`, `create_reminder` |
| destructive | runs | asks y/N first | `quit_app`, `sleep_computer`, `set_wifi` |
| always_ask | **asks every time** | asks every time | unused today; for tools that should never run unattended |
| blocked | never runs, hidden from the model | same | set per tool in `config.toml` |

A tool with no annotations counts as destructive.

`run_shell` exists after an experiment in going without it: with no shell tool, Sommus typed a command
into Terminal with `type_text` instead — same power, no output, no exit code, no log, and it reported
success for something that never ran. A real tool is the safer of the two. AppleScript still receives
user text through `argv`, never string formatting, and every call is logged.

## Setup

Needs macOS, Python 3.12, and [uv](https://docs.astral.sh/uv/) (`brew install uv`).

```bash
uv sync
cp .env.example .env        # then paste your model API key into .env
uv run sommus check         # verifies the key, nodes, and macOS permissions
uv run sommus
```

To run it from any folder, add a shell function (this is what `sommus` means elsewhere in this README):

```bash
echo 'sommus() { uv run --quiet --project "'"$PWD"'" sommus "$@"; }' >> ~/.zshrc
```

**macOS Accessibility permission** (for `media_control`, `press_keys` and `lock_screen`): System Settings →
Privacy & Security → Accessibility → enable the terminal app you run Sommus from, then restart it.
Reminders, Messages and Spotify/Music prompt separately the first time they're used (Privacy & Security →
Automation). **Screen Recording** is needed for `screenshot`, and reading a Chrome tab's *text* needs
Chrome's View → Developer → Allow JavaScript from Apple Events (titles and URLs work without it).

In the chat: `/tools`, `/cost`, `/new`, `/quit`. Ctrl+C cancels a reply.

Test a tool directly, no AI or API key needed:

```bash
uv run sommus tool                      # list tools
uv run sommus tool set_volume level=20
uv run sommus tool notify title=Hi message="From Sommus"
```

### Use the laptop node from any MCP client

The node is a standard MCP server, so any MCP-capable app can use it without the brain. Point the
client at:

```
command: .venv/bin/python
args:    -m sommus.nodes.laptop.server
```

## Scoring it

`evals/commands.toml` holds the 20 commands Phase 1 has to handle. The runner replays each one in a
fresh conversation and checks which tools were called:

```bash
uv run sommus eval                  # read tools run for real, the rest are simulated
uv run sommus eval --only volume    # just the commands mentioning "volume"
uv run sommus eval --live           # really run every tool (it will sleep the laptop)
```

Currently **46/46** on Sonnet 5 at low effort, ~$0.0095 per command. Target: never below 90%. Each run is saved to `data/evals/` with the tools called, replies, latency and cost,
so model and effort changes can be compared.

## Layout

```
src/sommus/
├── brain/        loop.py · nodes.py · permissions.py · prompt.py · store.py
├── interfaces/   cli.py
├── evals/        runner.py
└── nodes/laptop/ server.py (MCP tools) · macos.py (system) · apps.py (music, Shortcuts, Reminders) · files.py
evals/commands.toml   the 20 commands Phase 1 must handle
tests/                agent loop + permission gate against a scripted fake model and a real in-process node
```

## Development

```bash
uv run pytest
uv run ruff check src tests && uv run ruff format src tests
```

Every turn and tool call is logged to `data/sommus.db` (SQLite) with tokens and cost.
