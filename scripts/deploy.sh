#!/usr/bin/env bash
# Deploy origin/main to the always-on server without leaving Sommus down.
#
# Sommus is meant to run 24/7 (Jashan, 2026-11-09), so a deploy checks before and after the restart: the new code
# must import, and /health must answer within a minute. If either fails, the server goes back to the commit it
# was on and restarts that. Run from the Mac after pushing: scripts/deploy.sh
set -euo pipefail

ssh -o ConnectTimeout=15 sommus-brain 'bash -s' <<'REMOTE'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd ~/sommus
prev=$(git rev-parse HEAD)
git pull --ff-only -q
now=$(git rev-parse --short HEAD)
if [ "$(git rev-parse HEAD)" = "$prev" ]; then echo "Already at $now: nothing to deploy."; exit 0; fi
uv sync -q
back() {
  echo "Rolling back to ${prev:0:7}: $1"
  git reset -q --hard "$prev"
  uv sync -q || true
  sudo -n systemctl restart sommus
  exit 1
}
uv run --no-sync python -c "import sommus.server, sommus.activity, sommus.supplements" >/dev/null || back "the new code doesn't import"
sudo -n systemctl restart sommus
for i in $(seq 1 30); do
  sleep 2
  if curl -sf -m 3 http://100.101.226.81:8780/health >/dev/null; then
    echo "Deployed $now, healthy after $((i * 2)) s."
    exit 0
  fi
done
back "no /health answer within 60 s"
REMOTE
