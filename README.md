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
| read | runs | runs | `get_battery`, `list_apps` |
| reversible | runs | runs | `set_volume`, `open_app`, `lock_screen` |
| destructive | runs | asks y/N first | `quit_app`, `sleep_computer` |
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

**macOS Accessibility permission** (for `media_control` and `lock_screen`): System Settings →
Privacy & Security → Accessibility → enable the terminal app you run Sommus from, then restart it.

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

Target: 18/20. Each run is saved to `data/evals/` with the tools called, replies, latency and cost,
so model and effort changes can be compared.

## Layout

```
src/sommus/
├── brain/        loop.py · nodes.py · permissions.py · prompt.py · store.py
├── interfaces/   cli.py
├── evals/        runner.py
└── nodes/laptop/ server.py (MCP tools) · macos.py (actions)
evals/commands.toml   the 20 commands Phase 1 must handle
tests/                agent loop + permission gate against a fake Claude and a real in-process node
```

## Development

```bash
uv run pytest
uv run ruff check src tests && uv run ruff format src tests
```

Every turn and tool call is logged to `data/sommus.db` (SQLite) with tokens and cost.
