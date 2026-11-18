# 14. Sommus's own small model: a nightly data pipeline now, the fine-tune when the hardware exists

**Status:** accepted, 2026-11-18. Revisits [4](0004-no-local-llm.md) for one narrow job.

## Context
Jashan wants a small model of his own (1 to 3 billion parameters, LoRA fine-tuned) that handles the easy requests
(pick the tool and its arguments for fan, volume, reminders, supplements, next class, calendar) and hands everything
else to Claude, which stays the parent. It should train every night from every interaction plus fresh test data,
and he should always be told the progress.

Hardware today: the always-on server is a 1 GB Oracle VM with no GPU; the Mac is an 8 GB M1 Air that also runs
voice, Chrome and the rest. A 1.5B 4-bit LoRA fine-tune wants about 16 GB. So the fine-tune cannot run nightly yet.
ADR 4's reasons still hold for the *brain*: this model never replaces Claude, it only answers what it is sure of,
and a wrong action is scored worse than handing off.

## Decision
`sommus model-nightly` (also `python -m sommus.modeltrain`) runs six stages; each logs what it did, a failing stage
is recorded and the rest still run.

| Stage | Runs now? | What it does |
|---|---|---|
| 1 Export | yes, anywhere | New turns and tool calls since the last run (manifest `last_turn_id`), from `data/sommus.db`. PIN, diary and job turns skipped; requests or arguments that look like secrets (PINs, passwords, tokens, 8+ digit runs) dropped; outputs redacted and clipped. A turn still running holds the pointer back. |
| 2 Split | yes | `sha256(normalised request) % 100 < 20` is test. A request never moves between sets, and the files are append-only, so the test set only grows. |
| 3 Synthetic | yes, needs `ANTHROPIC_API_KEY` | Haiku writes new requests with their tool calls for the 10 least-covered tools. The prompt holds only tool schemas and generic instructions, never real text. Each example is checked against the real schemas in `data/nodes/*.json` (tool, argument names, types, required) and dropped if invalid. Worst-case cost is estimated before every call; the cap is $0.05 a night. Spend is written to the main log so the monthly budget counts it. |
| 4 Train | **no** (skipped, with the exact reason) | Only on Apple silicon with mlx-lm and enough memory: `python -m mlx_lm.lora` on train.jsonl (real + synthetic train sets). Otherwise `skipped: needs Apple silicon (this machine: Linux x86_64, 1 GB RAM)`. Also skipped with too few examples or nothing new. |
| 5 Evaluate | baseline yes; candidate later | Per held-out example: same tool, same arguments, and an escalation-aware score (+1 right action or right escalation, 0 for escalating something it could do, -2 for acting wrongly). Real and synthetic sets are scored separately. The baseline "always escalate" is always reported. |
| 6 Report | yes | One JSON line in `data/training/progress.jsonl`, a one-line summary (stdout of the command), and a dated section appended to the vault note `Portfolio/Projects/Sommus Model Progress.md`. |

What the model should answer: the target is `{"calls": [...]}` when Claude handled the request with one or two
working tool calls, else `{"escalate": true}` (no tool, a long chain, an error, or a hand-off like `ask_claude`).

Files in `data/training/` (gitignored): `manifest.json`, `real_train|real_test|synthetic_train|synthetic_test.jsonl`,
`progress.jsonl`, `nightly.log`, and when training runs `mlx/` (dataset), `lora.yaml`, `adapters/<date>/`.

## Running it
By hand: `uv run --no-sync sommus model-nightly` (stage log on stderr, the summary on stdout).

Server cron (it only needs Python and the log, no GPU): `crontab -e`, then

    30 3 * * * /home/ubuntu/sommus/scripts/model-nightly.sh

The script keeps `data/training/nightly.log`, always exits 0, and sends the summary to Telegram when
`SOMMUS_NIGHTLY_TELEGRAM=1` (in `.env`), using the bot token and first id in `TELEGRAM_ALLOWED_IDS`. A cleaner hook
later is to call `nightly.run_from_config()` from `sommus serve` next to the diary and send the summary through the
bot it already holds. 03:30 is also the diary hour; shift it by 15 minutes if the 1 GB box feels it.

Settings: `[model_training]` in `config.toml`. `enabled = false` turns the whole job off; `train = false` keeps the
data stages but never fine-tunes.

## What it will not do yet, honestly
- **No model is trained today.** Every report says `training: skipped (needs 16 GB Apple silicon)` until the job
  runs on suitable hardware (a 16 GB+ Apple silicon Mac, or a rented one). Nothing is reported as trained that was not.
- The MLX command line and the candidate-model loader (`evaluate.load_mlx_model`) are written against mlx-lm's
  documented interface but have never been run: the first real run on a Mac with mlx-lm will need a look.
- Tool schemas come from `data/nodes/*.json`, which the brain caches only for Mac-side (remote) nodes. Tools from
  nodes that run locally on the server (vault, gmail, home, web) are not in those files, so synthetic data does not
  cover them until their lists are cached too.
- Real data is mostly what Claude did, not ground truth: if Claude picked a wrong tool the model learns it. The
  eval measures agreement with Claude, not correctness.
- A cheap model writes the synthetic data from schemas, so its requests are cleaner than real speech. That is why
  real and synthetic scores are kept apart; only the real score says anything about use.
- Per night the data grows slowly (tens of real requests); a small LoRA needs hundreds. Expect weeks before the
  numbers mean anything.

## Revisit when
A Mac with 16 GB or more, or a cheap rented GPU hour per week, is available: then run stage 4 there and let the
eval decide whether the model is allowed to answer anything. A model goes live only if its real-set wrong-action rate
is near zero; that gate is a separate change.
