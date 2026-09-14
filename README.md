# Sommus

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

## What it can do

**53 tools across three nodes**, plus live web search:

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
| Downloads | fetch a file straight to disk |
| Messaging | send an iMessage (always confirms first), write or send a Gmail |
| Escape hatch | run any of the user's macOS **Shortcuts** — Focus modes, Home devices, anything macOS won't script |
| Knowledge | **web search** for weather, news, prices, anything after the model's cutoff |

It answers questions as readily as it acts, and when there's no exact tool it tries the nearest route
(a Shortcut, a keystroke, opening the right settings pane) before saying it can't.

## Nodes

| Node | Tools | Setup |
|---|---|---|
| **laptop** | 41 — sound, display, apps, browser, screen, files, clipboard, reminders, shortcuts | macOS permissions (below) |
| **vault** | 6 — search, read, list, append, add a to-do, commit the notes repo | none |
| **gmail** | 6 — search, read, send, reply, draft, mark read | app password in `.env` |

A node that isn't set up reports as unreachable; everything else keeps working.

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

`uv run sommus check` verifies the login. Without it, email still works through the browser
(`compose_email`) and the Gmail tools just explain the setup.

## Architecture

```
  INTERFACES                      BRAIN                         NODES
  ┌──────────────┐          ┌──────────────────────┐   MCP    ┌──────────────────┐
  │ terminal     │─ text ─► │ agent loop           │ ───────► │ laptop (macOS)   │
  │ voice (next) │ ◄ events │ Claude API           │ ───────► │ bluetooth (next) │
  └──────────────┘          │ permission gate      │          └──────────────────┘
                            │ audit log + cost     │
                            └──────────────────────┘
```

- **Interfaces** only exchange text and events with the brain. Voice will be a new interface, not a rewrite.
- **Nodes** are [MCP](https://modelcontextprotocol.io) servers. Each device lists its tools; the brain routes calls.
  A new device is a new node.
- **The agent loop** is hand-written on the Claude Messages API (`src/sommus/brain/loop.py`): streaming,
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
| always_ask | **asks every time** | asks every time | `send_message` — it reaches another person |
| blocked | never runs, hidden from the model | same | set per tool in `config.toml` |

A tool with no annotations counts as destructive. There is deliberately no shell tool: every action
takes typed arguments, and AppleScript gets user text through `argv`, never string formatting.

## Setup

Needs macOS, Python 3.12, and [uv](https://docs.astral.sh/uv/) (`brew install uv`).

```bash
uv sync
cp .env.example .env        # then paste your Anthropic API key into .env
uv run sommus check         # verifies the key, nodes, and macOS permissions
uv run sommus
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

### Use the laptop node from Claude Desktop or Claude Code

The node is a standard MCP server, so it works without the brain:

```bash
claude mcp add sommus-laptop -- "$(pwd)/.venv/bin/python" -m sommus.nodes.laptop.server
```

## Scoring it

`evals/commands.toml` holds the 20 commands Phase 1 has to handle. The runner replays each one in a
fresh conversation and checks which tools were called:

```bash
uv run sommus eval                  # read tools run for real, the rest are simulated
uv run sommus eval --only volume    # just the commands mentioning "volume"
uv run sommus eval --live           # really run every tool (it will sleep the laptop)
```

Currently **42/42** on Sonnet 5 at low effort, ~$0.009 per command. Target: never below 90%. Each run is saved to `data/evals/` with the tools called, replies, latency and cost,
so model and effort changes can be compared.

## Layout

```
src/sommus/
├── brain/        loop.py · nodes.py · permissions.py · prompt.py · store.py
├── interfaces/   cli.py
├── evals/        runner.py
└── nodes/laptop/ server.py (MCP tools) · macos.py (system) · apps.py (music, Shortcuts, Reminders) · files.py
evals/commands.toml   the 20 commands Phase 1 must handle
tests/                agent loop + permission gate against a fake Claude and a real in-process node
```

## Development

```bash
uv run pytest
uv run ruff check src tests && uv run ruff format src tests
```

Every turn and tool call is logged to `data/sommus.db` (SQLite) with tokens and cost.
