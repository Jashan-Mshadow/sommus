#!/usr/bin/env bash
# Train Sommus's own small model on THIS Mac (Apple silicon, 16 GB or more: the new M5 Pro).
#
# The server collects the real requests and writes the synthetic ones every night, but it is a 1 GB Linux box, so its
# nightly job says "training: skipped (needs 16 GB Apple silicon)". This copies that data here, then fine-tunes the
# model and scores it against Claude. Nothing is exported from this Mac's own log and no API money is spent.
#
#   scripts/model-train-mac.sh            # copy the server's data, train, score, write the vault note
#   scripts/model-train-mac.sh --no-sync  # train on what is already in data/training
#
# First run: mlx-lm is installed (one time) and the first fine-tune will probably need a fix (it has never run).
# Result: Portfolio/Projects/Sommus Model Training (Mac).md in the vault, and the adapter in data/training/adapters/.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

if [ "$(uname -s)-$(uname -m)" != "Darwin-arm64" ]; then
  echo "This needs an Apple-silicon Mac (this one is $(uname -s) $(uname -m))."; exit 1
fi
if ! uv run --no-sync python -c "import mlx_lm" 2>/dev/null; then
  echo "Installing mlx-lm (one time)..."
  uv pip install mlx-lm
fi

if [ "${1:-}" != "--no-sync" ]; then
  mkdir -p data/training data/nodes
  # Only the data. This Mac keeps its own manifest, progress log and adapters, so a sync never undoes a training run.
  rsync -az --exclude manifest.json --exclude progress.jsonl --exclude nightly.log --exclude adapters --exclude mlx \
    sommus-brain:sommus/data/training/ data/training/
  rsync -az sommus-brain:sommus/data/nodes/ data/nodes/ || true   # the server-side tool lists the examples are checked against
  echo "Copied the server's training data: $(wc -l < data/training/real_train.jsonl) real and $(wc -l < data/training/synthetic_train.jsonl) synthetic training examples."
fi

uv run --no-sync sommus model-nightly --train-only
