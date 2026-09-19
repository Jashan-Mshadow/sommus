# 4. No local LLM for the brain

**Status:** accepted, 2026-09-15

## Context
Running a model on the laptop would make the brain free and offline. The laptop is a MacBook Air M1
with 8 GB of RAM and little free disk; the voice stack (Whisper and Kokoro) already runs locally.

## Decision
The brain stays on the API. Local models are used only where they are small and single-purpose:
speech-to-text (mlx-whisper small.en, ~0.35 s) and text-to-speech (Kokoro-82M).

## Why
- Tool calling across 60 tools with deferred loading is where small local models are weakest, and a
  wrong tool call here quits apps or sends messages.
- 8 GB is shared with the voice models, Chrome and everything else.
- The cheap path already exists and is better: the fast path answers everyday commands with no model
  at all ($0, ~0.1 s), and the API handles the rest at under a cent.

## Revisit when
Apple's on-device model is available on this Mac (it needs macOS 26), or a small open model passes the
eval at a level close to Sonnet's.
