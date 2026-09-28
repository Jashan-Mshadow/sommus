# 8. Cache the conversation only while it's live, for five minutes

**Status:** accepted, 2026-09-28 — savings to be confirmed with `/cost` (cache share) over a week of use

## Context
The system prompt (~6k tokens) is cached for an hour, which fits phone commands that arrive far apart.
Everything after it was either not cached at all (the history of earlier turns, re-sent at full price on
every turn) or cached for an hour (the newest tool results inside a turn, written at 2x even though the
next step is seconds away). History was also trimmed one turn at a time, which changes its start and
throws away any cached copy on every turn once the window is full.

## Decision
- Tool results inside a turn: 5-minute breakpoint (1.25x write) instead of 1 hour (2x).
- A turn that starts within 4 minutes of the last one (or any spoken turn) puts a 5-minute breakpoint on
  its own request, so the next turn reads the whole history before it at 0.1x. A turn long after the last
  doesn't: its cache would expire before anything read it.
- History is trimmed to half the window at once, so its start stays the same for several turns.
- Plain-text messages go to the API as text blocks, so the history is byte-for-byte the same between turns.

## Consequences
- Breakpoints per request: system (1h), this turn's request (5m), newest tool results (5m). Longer TTLs
  come first, as the API requires.
- Worth most in voice conversations, where turns come seconds apart and the history grows quickly.
