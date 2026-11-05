# Decision records

Short write-ups of the choices that shaped Sommus: the context, what was decided, and what it cost.
The numbers behind them are in [MEASUREMENTS.md](../MEASUREMENTS.md) (regenerate with `sommus stats`).

| # | Decision |
|---|---|
| [1](0001-nodes-are-mcp-servers.md) | Every device is an MCP server ("node") |
| [2](0002-permission-tiers-and-pin.md) | Safety lives in code: permission tiers plus a PIN for personal actions |
| [3](0003-sonnet-at-low-effort.md) | Sonnet 5 at low effort, chosen by measurement |
| [4](0004-no-local-llm.md) | No local LLM for the brain |
| [5](0005-one-model-with-a-budget-guard.md) | One model plus a monthly budget guard, instead of per-request routing |
| [6](0006-fast-path-and-campus-engine.md) | Answer everyday requests without a model |
| [7](0007-outside-content-cannot-trigger-outward-actions.md) | Outside content can't trigger outward actions on its own |
| [8](0008-short-lived-cache-for-the-conversation.md) | Cache the conversation only while it's live, for five minutes |
| [9](0009-hearing-from-across-the-room.md) | Hearing from across the room: a name hint for Whisper, Smart Turn, and a voice log |
| [10](0010-live-voice-on-gemini.md) | Live voice on Gemini Live, with Sommus keeping the wake word, the tools and the PIN |
| [11](0011-a-nightly-diary-instead-of-mem0.md) | Long-term memory is a nightly diary distilled from the log, not Mem0 |
| [12](0012-small-mac-actions-wait-for-the-mac.md) | Small Mac actions wait for the Mac to wake; nothing else does |
