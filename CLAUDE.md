# Sommus

Personal assistant that controls Jashan's devices: one brain (hand-written agent loop on the Anthropic
Messages API), interfaces in front (terminal, Telegram, voice), nodes behind (MCP servers over stdio).

Full working memory — decisions, gotchas, current state — is the vault note
`~/Documents/Jashans_Brain/Portfolio/Projects/Sommus — Session Handoff.md`. Read it before non-trivial work.
Roadmap: `Sommus — Build Plan.md` in the same folder.

## Commands

```bash
uv run pytest -q                      # all tests; no API calls, no audio
uv run ruff check src tests && uv run ruff format src tests
sommus                                # terminal + Telegram (zsh function; works from any folder)
sommus tool [name k=v]                # run a tool directly, no model, $0
sommus eval --only "<text>"           # one eval case (~1¢). The full eval is ~40–50¢: don't run it casually
sommus today                          # today's classes + deadlines, writes data/today.json ($0)
sommus stats                          # regenerate docs/MEASUREMENTS.md from the log ($0)
```

Decisions and their reasons: `docs/decisions/`. Add one when a choice is measured or hard to reverse.

## Map

| Path | Role |
|---|---|
| `src/sommus/brain/loop.py` | agent loop: streaming, caching, tool search, history trim, fast-path executor |
| `src/sommus/brain/fastpath.py` | $0 intent matcher — an intent claims a request only if *every* word is in its vocabulary |
| `src/sommus/brain/campus.py` | $0 schedule engine: next class, today, today.json |
| `src/sommus/brain/budget.py` | monthly spend guard; cheap model past 90% of the cap (no per-request routing: see docs/decisions/0005) |
| `src/sommus/brain/memory.py` | long-term facts (remember / forget) loaded into the prompt |
| `src/sommus/brain/prompt.py` | system prompt |
| `src/sommus/stats.py` | `sommus stats`: aggregates only, never request text (the page is public) |
| `src/sommus/brain/store.py` | SQLite log of turns, tool calls, cost |
| `src/sommus/brain/pin.py` · `permissions.py` | PIN gate for personal tools · permission tiers |
| `src/sommus/nodes/<node>/server.py` | MCP servers: laptop, vault, gmail, web, claude |
| `src/sommus/interfaces/` | cli, telegram, voice, duplex (barge-in) |
| `src/sommus/evals/runner.py` + `evals/commands.toml` | scored eval |
| `config.toml` | all settings · `profile.md` (gitignored) facts about Jashan · `.env` secrets |
| `tests/fakes.py` | FakeModel, fake node, test config |

## Rules

- **No AI attribution anywhere**: no Co-Authored-By trailers, no "generated with", no assistant mentions in
  commits, docs, comments or tests. Model ids in config are fine.
- **Cost ceiling ≈ $10/month.** Measure before claiming savings. Prefer the fast path or free data APIs over the model.
- **Grow the fast path from real use** (`/candidates` in `sommus`). Add must-match *and* must-not-match tests.
- Tests never make API calls or play sound (silent buffers only).
- MCP SDK is 2.x: `from mcp.server.mcpserver import MCPServer`, snake_case fields. Node errors → `ToolError(...)`.
- This Python has no CA certificates: use `curl` or `httpx2`, not `urllib`, for HTTPS.
- Secrets live in `.env`; never print or commit them. Jashan does account setup and pastes secrets himself.
- Commit messages explain *why*; push after each meaningful change.
