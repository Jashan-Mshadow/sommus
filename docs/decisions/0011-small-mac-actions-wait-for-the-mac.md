# 11. Small Mac actions wait for the Mac to wake; nothing else does

**Status:** accepted, 2026-11-03

## Context
With the brain on the server, a Mac tool asked while the laptop sleeps answered "the MacBook isn't reachable"
after ~4 s, and the request was lost. For some actions that is the right answer (volume, a screenshot, anything
he wants now), but for a reminder in Apple's Reminders or a page to open "later" it just made him ask again.

## Decision
- A tool waits on the server only if it is listed in `[mac_queue] tools`, is in the reversible tier (after
  `[permissions]` overrides), and is not in `[security] pin_tools`. All three are checked in code
  (`brain/later.py`, `NodeHub._wait_or_fail`), so a careless edit of the list can't queue `send_message` or
  `run_shell`.
- Queued calls are kept in `data/mac_queue.json` (a restart keeps them), deduplicated, and run by the server's
  30 s scheduler once the Mac's node is connected again. Jashan gets one Telegram message with what ran and
  what failed.
- Anything older than `hours` (24) is dropped and reported, not run: opening a page two days late is worse
  than not opening it.

## Consequences
- The tool result tells the model the action is queued (not an error), so it says so instead of trying
  another tool.
- If the Mac drops off *while* a call is in flight, that call may have run before the failure was seen; a
  queued retry can then run it twice. Only reversible tools wait, so the worst case is a duplicate reminder.
- Personal actions still need him there: the PIN gate decides at the time of asking, and nothing gated is
  replayed later without him.
