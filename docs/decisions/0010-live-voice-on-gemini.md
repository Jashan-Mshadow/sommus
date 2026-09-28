# 10. Live voice on Gemini Live, with Sommus keeping the wake word, the tools and the PIN

**Status:** accepted, 2026-09-28 — `sommus live` beside `sommus voice`, not replacing it

## Context
After the first real conversation Jashan said voice "works fully" but doesn't feel as dynamic as ChatGPT or
Claude voice. `sommus voice` is a relay (Whisper → Claude → Kokoro): ~2–4 s before the first word, 10 s+ when
Claude runs a tool, and speech that sounds read aloud. Open-source assistants (Home Assistant Assist,
OpenJarvis) use the same relay; the open speech-to-speech models (Kyutai Moshi) need a large GPU and are weak
at tools. Paid options (OpenAI Realtime) cost cents a minute, over the ~$10/month ceiling in daily use.

## Decision
`sommus live` uses **Gemini 3.8 Live** (speech in, speech out), free on the AI Studio key. Measured end to
end on 2026-09-28 with the real brain and nodes (headless, no sound): first audio 0.75 s for small talk,
1.5 s for the battery through a Sommus tool, 1.3 s for the next class through `ask_sommus`.

Sommus keeps, on the Mac:
- **Wake word** — local Silero + Whisper, as in `sommus voice`. Nothing is sent to Google before it; the session
  closes after 30 s of quiet or "that's all".
- **Tools** — Gemini calls quick, non-personal ones directly; everything else goes through `ask_sommus`, the
  Claude brain (fast path first, so everyday questions stay $0). All through `Brain.call_tool` / `handle`:
  permission tiers, PIN gate, outside-content rule.
- **PIN** — a locked action switches the mic from Google to local Whisper until the digits are heard.
- **Echo cancellation** — replies stream through the same voice-processing engine, so Gemini doesn't hear
  itself; its own voice-activity detection handles interruptions, set to low sensitivity.

## Consequences
- The voice is one of Gemini's ~30 (it can't return text for Kokoro); `[live] style` steers accent and tone.
- Needs internet. Devices don't need to share a network: each connects out, so this moves to the cloud brain
  unchanged (the brain holds the session and tools; devices only stream audio).
- Free-tier conversations may be used by Google to improve its products; the PIN and anything before the wake
  word never reach it. Free-tier limits aren't published; if a session fails to connect, `sommus voice` is the
  fallback.
- Claude's conversation history doesn't include Gemini's small talk; Gemini passes context in `ask_sommus`.
