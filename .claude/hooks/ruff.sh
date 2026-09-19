#!/bin/sh
# After Claude Code edits a Python file: format it, then lint it. Lint errors go back to
# Claude (exit 2) so it fixes them in the same turn instead of a later round trip.
file=$(jq -r '.tool_input.file_path // empty')
case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0
cd "$CLAUDE_PROJECT_DIR" || exit 0
uv run --quiet ruff format --quiet "$file"
if ! out=$(uv run --quiet ruff check --fix "$file" 2>&1); then
  echo "$out" >&2
  exit 2
fi
