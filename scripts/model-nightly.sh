#!/usr/bin/env bash
# Nightly run of Sommus's own-model pipeline (docs/decisions/0014-own-model-nightly.md). For the server's cron.
# Keeps a log, never fails loudly (always exits 0), and can send the one-line summary to Telegram.
#   cron:  30 3 * * *  ~/sommus/scripts/model-nightly.sh
# SOMMUS_NIGHTLY_TELEGRAM=1 (in .env or the crontab) sends the summary with the bot already in .env.
cd "$(dirname "$0")/.." || exit 0
[ -e data/sleep.json ] && exit 0  # hard sleep (sleepmode.py): Sommus does nothing, not even this job
export PATH="$HOME/.local/bin:$PATH"
mkdir -p data/training
log=data/training/nightly.log
envval() { grep -E "^$1=" .env 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d "\"'"; }
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(envval TELEGRAM_BOT_TOKEN)}"
TELEGRAM_ALLOWED_IDS="${TELEGRAM_ALLOWED_IDS:-$(envval TELEGRAM_ALLOWED_IDS)}"
SOMMUS_NIGHTLY_TELEGRAM="${SOMMUS_NIGHTLY_TELEGRAM:-$(envval SOMMUS_NIGHTLY_TELEGRAM)}"
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
  summary=$(uv run --no-sync sommus model-nightly 2>>"$log")
  echo "$summary"
} >>"$log" 2>&1
summary=$(tail -n 40 "$log" | grep '^Night ' | tail -n 1)
if [ "${SOMMUS_NIGHTLY_TELEGRAM:-0}" = "1" ] && [ -n "$summary" ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  chat="${TELEGRAM_ALLOWED_IDS%%,*}"
  [ -n "$chat" ] && curl -sf -m 20 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=$chat" --data-urlencode "text=$summary" >/dev/null 2>&1
fi
tail -n 2000 "$log" >"$log.tmp" 2>/dev/null && mv "$log.tmp" "$log"
exit 0
