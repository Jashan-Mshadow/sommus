#!/usr/bin/env bash
# Set or change the hard-sleep phrases (src/sommus/sleepmode.py) on the Mac and the server, then deploy and restart.
#
# The phrases are secrets and this repo is public. They are typed here (hidden) and written only to .env
# (gitignored, next to the API keys) on the Mac and the server, as SOMMUS_SLEEP_PHRASE / SOMMUS_WAKE_PHRASE. They are
# plain text there on purpose: speech-to-text writes an invented word differently every time, so voice matching
# compares the words loosely and the digits exactly, which a hash cannot do. Nothing is printed or put in git.
# Run it yourself in a terminal:   scripts/set-sleep-phrases.sh
set -euo pipefail
cd "$(dirname "$0")/.."

read -rsp "Sleep phrase (e.g. sleepmax override 0000): " sleep_phrase; echo
read -rsp "Wake phrase: " wake_phrase; echo
[ -n "$sleep_phrase" ] && [ -n "$wake_phrase" ] || { echo "Both phrases are needed."; exit 1; }
case "$sleep_phrase$wake_phrase" in *\"*|*\\*|*\$*|*\`*) echo "Use only letters, digits, spaces and : - . in a phrase."; exit 1;; esac
if [ "$sleep_phrase" = "$wake_phrase" ]; then echo "The two phrases are the same. Pick two different ones."; exit 1; fi

lines=$(printf 'SOMMUS_SLEEP_PHRASE="%s"\nSOMMUS_WAKE_PHRASE="%s"\n' "$sleep_phrase" "$wake_phrase")
unset sleep_phrase wake_phrase
drop='^SOMMUS_(SLEEP|WAKE)_(PHRASE|HASH)='   # also removes the older hash lines

grep -Ev "$drop" .env >.env.new || true
printf '%s\n' "$lines" >>.env.new
cat .env.new >.env && rm .env.new
printf '%s\n' "$lines" | ssh sommus-brain "cd ~/sommus && { grep -Ev '$drop' .env || true; cat; } >.env.new && cat .env.new >.env && rm .env.new"
unset lines
echo "Phrases saved on the Mac and the server."

out=$(scripts/deploy.sh 2>&1) || { echo "$out"; exit 1; }   # restarts the server when there is new code
echo "$out"
case "$out" in
  *"nothing to deploy"*)  # no new code, so no restart: the server only reads .env when it starts
    ssh sommus-brain 'sudo -n systemctl restart sommus; for i in $(seq 1 30); do sleep 2; if curl -sf -m 3 http://100.101.226.81:8780/health >/dev/null; then echo "Server restarted, healthy after $((i * 2)) s."; exit 0; fi; done; echo "The server did not answer after the restart."; exit 1'
    ;;
esac
# Restart the Mac's live mode too: its wrapper starts a fresh one in 5 s.
pkill -f ".venv/bin/sommus live" 2>/dev/null || true
pkill -f "uv run --quiet --no-sync sommus live" 2>/dev/null || true
echo "Done. Try it on Telegram: send the sleep phrase, then the wake phrase."
