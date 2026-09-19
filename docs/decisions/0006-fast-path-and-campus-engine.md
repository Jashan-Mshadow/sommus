# 6. Answer everyday requests without a model

**Status:** accepted, 2026-09-15 (fast path), 2026-09-19 (campus engine)

## Context
Most requests are the same few things: volume, brightness, music, battery, weather, "where's my next
class". Each one through the model cost 0.8–2.7¢ and took 3–4 s; calendar questions took 20–27 s.

## Decision
- **Fast path:** each request is reduced to words, and an intent claims it only when *every*
  remaining word is in that intent's vocabulary. "Brightness down 12" and "make the screen a bit
  dimmer" land on one intent; "set brightness to 20 and open Spotify" goes to the model. A wrong
  match does the wrong thing and a miss only costs what it did before, so the rule leans hard towards
  missing. Every intent has must-match and must-not-match tests.
- **Grow it from the log:** `/candidates` lists requests the model handled with a single tool, most
  frequent first. Those are the next intents.
- **Campus engine:** classes, rooms and deadlines come from a schedule file (weekly slots, one-off
  labs and extra lectures, reading week, midterms), not from two Google calendars.

## Consequences
- $0 and about 0.1 s for everyday commands; class questions went from 20–27 s to under a millisecond.
- Opening or quitting an app only matches an installed app's exact name, so "open my LEARN page"
  still reaches the model.
- The schedule file must be kept in sync with the real calendar; the morning brief checks it
  against UW Flow.
