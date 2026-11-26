#!/usr/bin/env bash
# Set or change the hard-sleep phrases (src/sommus/sleepmode.py) on the Mac and the server, then deploy and restart.
#
# The phrases are secrets and this repo is public: they are typed here, hidden, turned into salted hashes, and only
# the hashes go into .env (SOMMUS_SLEEP_HASH / SOMMUS_WAKE_HASH). Nothing is stored or printed. Run it yourself in a
# terminal:   scripts/set-sleep-phrases.sh
set -euo pipefail
cd "$(dirname "$0")/.."

read -rsp "Sleep phrase (say it however you like, e.g. sleepmax override 0000): " sleep_phrase; echo
read -rsp "Wake phrase: " wake_phrase; echo
[ -n "$sleep_phrase" ] && [ -n "$wake_phrase" ] || { echo "Both phrases are needed."; exit 1; }

sleep_hash=$(printf '%s' "$sleep_phrase" | uv run --no-sync sommus sleep-hash -)
wake_hash=$(printf '%s' "$wake_phrase" | uv run --no-sync sommus sleep-hash -)
unset sleep_phrase wake_phrase
if [ "$sleep_hash" = "$wake_hash" ]; then echo "The two phrases are the same. Pick two different ones."; exit 1; fi

set_line() {  # $1 = file, $2 = KEY, $3 = value: replace the line, or add it
  if grep -q "^$2=" "$1" 2>/dev/null; then sed -i.bak "s|^$2=.*|$2=$3|" "$1" && rm -f "$1.bak"; else printf '\n%s=%s\n' "$2" "$3" >>"$1"; fi
}
set_line .env SOMMUS_SLEEP_HASH "$sleep_hash"
set_line .env SOMMUS_WAKE_HASH "$wake_hash"
ssh sommus-brain "cd ~/sommus && $(declare -f set_line) && set_line .env SOMMUS_SLEEP_HASH '$sleep_hash' && set_line .env SOMMUS_WAKE_HASH '$wake_hash'"
echo "Hashes saved on the Mac and the server."

scripts/deploy.sh   # restarts the server so it reads them (and picks up any new code)
# Restart the Mac's live mode too: its wrapper starts a fresh one in 5 s.
pkill -f ".venv/bin/sommus live" 2>/dev/null || true
pkill -f "uv run --quiet --no-sync sommus live" 2>/dev/null || true
echo "Done. Try it on Telegram: send the sleep phrase, then the wake phrase."
