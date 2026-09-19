# 2. Safety lives in code: permission tiers plus a PIN for personal actions

**Status:** accepted, 2026-09-14 (tiers), 2026-09-15 (PIN)

## Context
Sommus sends email and messages, reads files and runs shell commands. Prompt instructions are not a
security boundary: tool output, web pages or someone talking near the laptop can steer the model.

## Decision
- Every tool has a tier — read, reversible, destructive, blocked — enforced by the brain before the
  call, never by the prompt. Unannotated means destructive.
- 35 personal tools (email, messages, contacts, notes, files, browser contents, shell, screen control)
  are locked until the PIN is given. The PIN is caught before the fast path and the model, never
  stored in history or logs, and kept only as a salted scrypt hash. Unlock lasts 10 minutes and is
  shared across interfaces; three wrong attempts lock it for five.
- Long-term memory refuses anything that looks like a secret, in code.

## Consequences
- Everyday commands (volume, apps, music, weather, class times) stay instant and never ask.
- A roommate saying "hey Sommus, email my prof" gets asked for a PIN it can't give.
- Planned: speaker recognition replaces the PIN, which stays as the override.
