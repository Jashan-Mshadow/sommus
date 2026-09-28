# 9. Hearing from across the room: a name hint for Whisper, Smart Turn, and a voice log

**Status:** accepted, 2026-09-28 — to be confirmed by real use; `data/voice.log` records every utterance

## Context
Jashan: "it doesn't listen to half of what I say", worst from a few steps away. Two causes were bugs, now
fixed: a lone number while locked was swallowed as a PIN attempt with no reply, and replies blocked the
microphone loop, so interrupting didn't work until the reply was over. The rest is how well speech is
caught and read.

## Measured (2026-09-28, M1, headless — no sound played)
Kokoro speech in four voices, made quiet (-42 dB RMS), given room echo (RT60 0.5 s) and background noise:

| Whisper | Name hint | Woke (8 phrases) | Words right | Time |
|---|---|---|---|---|
| small.en | no | 6/8 | 71% | 0.49 s |
| small.en | **"Hey Sommus."** | **8/8** | **88%** | **0.43 s** |
| large-v3-turbo | no | 7/8 | 88% | 1.64 s |
| large-v3-turbo | yes | 8/8 | 88% | 1.64 s |

False wakes with the hint, on 8 lines of room talk plus hiss, hum, typing and silence: 1/12, the same as
without it (the one is "So, miss, …", accepted on purpose).

## Decision
- Keep small.en; pass `initial_prompt="Hey Sommus."`. The bigger model is 3.4x slower for nothing more.
- **Smart Turn v3.2** (pipecat-ai/smart-turn, BSD-2, 9 MB ONNX, ~55 ms) ends a turn at a 0.4 s pause when it
  hears a finished sentence, and waits up to 1.6 s when it doesn't. On synthetic speech its scores were
  mixed (TTS always ends with a falling tone, even on "Remind me to"), so the text check for sentences
  ending in "to/and/the…" stays as a backstop. `[voice] smart_turn = false` reverts to the fixed pause.
- Log every utterance (`data/voice.log`): loudness in dB, peak speech score, Smart Turn score, transcript,
  time to transcribe, outcome. The next "it missed me" is diagnosed from the log, not from memory.

## Looked at, not taken (yet)
- **openWakeWord** with a custom "Sommus" model: a dedicated wake-word model is sturdier than reading
  Whisper's text, but the official training notebook no longer runs (2022-era pins); community trainers
  (Kokoro- or Piper-generated samples) take ~1–2 h on a GPU. Worth it if the log shows missed wakes on
  loud or distant speech that Whisper still misreads.
- **Pipecat / LiveKit Agents** as the whole voice stack: built for WebRTC calls to a server, not an
  always-on local mic with a wake phrase and a PIN. Sommus takes the one piece that matters (Smart Turn).
- **OpenJarvis** (open-jarvis/OpenJarvis and others): local-first agent frameworks; nothing in their
  voice path beats what's here (Silero VAD + Whisper + Kokoro is the same stack most of them use).
