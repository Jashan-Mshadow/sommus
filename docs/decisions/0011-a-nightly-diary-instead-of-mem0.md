# 11. Long-term memory is a nightly diary, not Mem0

**Status:** accepted, 2026-11-03. The cost estimate below is from token counts; confirm it with `/cost` and the
`[diary]` rows in the log after a couple of weeks.

## Context
Sommus forgot everything between conversations except the facts Jashan explicitly asked it to keep
(`memory.py`). A Jarvis remembers that the midterm was Thursday and asks how it went. The memory has to run
on the Oracle free VM next to the service (1 GB RAM, ~270 MB used, `MemoryMax=600M`), keep the month under
about $10, add no paid services, and leave the cached system prompt alone (decision 8).

Two options:

**(a) Mem0, open source, as a library.** By default every `add()` makes an LLM call to pull facts out of the
conversation, embeds them, looks up similar memories in a vector store, and makes a second LLM call to decide
whether to add, update or delete. Anthropic has no embeddings API, so it needs a second provider (OpenAI or
Gemini keys) or a local embedding model. sentence-transformers plus torch is several hundred MB, which doesn't
fit in the ~330 MB left under the cap. It also brings a vector store (local Qdrant or Chroma) and its
dependencies.
- Cost when it runs on every turn: two extra calls per model turn. At about 30 turns a day and ~1.5k tokens a
  call on Haiku 4.5, that's around 10¢ a day, roughly $3/month, a third of the budget, spent before any
  request is answered.
- What retrieval returns changes from one turn to the next. In the system prompt it would rewrite the cache
  on every turn. In the message it costs the same as (b).
- Running it in a nightly batch saves on calls, but then it's option (b) with a heavy dependency and an
  extra provider added.

**(b) Home-grown nightly diary.** Once a night, the server sends the day's turns from the SQLite log
(Jashan's words and Sommus's replies, never tool output) to Haiku 4.5 in one call. It writes back:
- a short summary of the day,
- up to three things worth bringing up later ("follow-ups", with the first day they make sense),
- up to three lasting facts, which go through `memory.remember` and its secret filter,
- which open follow-ups the day settled.
Retrieval uses no embeddings: the last few days go into the system prompt, and an older day comes back only
when a request shares its uncommon words.

## Decision
Option (b). It is much cheaper, needs no extra RAM, provider or vector store, and fits the cache design. A
diary is also the right shape for "bring things up naturally": it gives dated events and follow-ups, not a
pile of facts.

- **Storage:** `Sommus Diary.md` in the private vault, next to `Sommus Memory.md`: one `## YYYY-MM-DD`
  section a day with `- Follow up [date]: …` lines. It is synced by the existing vault git job and readable and
  editable by hand. Nothing goes in the public repo.
- **Job:** the server's scheduler runs it at `[memory] diary_at` (03:30, in the schedule's time zone). It covers
  the turns since the last run, and a run before noon is labelled with the previous day. A day with no turns
  makes no model call. The call is logged as a `[diary]` turn, so the budget guard counts it, and the job is
  skipped once the month passes the cheap-model threshold.
- **Retrieval, cache-safe:**
  - The last 3 days (only those within a week) and up to 4 open follow-ups go at the *end* of the system
    prompt. The brain rebuilds the prompt only when the memory file, the diary or the date changes. The
    diary changes once a night, hours after the 1-hour cache has expired, so this adds no cache writes.
  - An older day matching the request is added as a one-line note on that turn's user message, after the
    cached prefix. The system prompt stays byte-for-byte the same.
- **Budget:** each day is stored at no more than 500 characters and each follow-up at no more than 200. The
  prompt block is at most ~2.3k characters (~600 tokens, read from cache at 0.1×). A recall note holds at
  most 2 days × 300 characters (~150 tokens), and only on requests that match.
- **Inspect / forget:**
  - The `recall_diary` vault tool (a day or words) and `list_memories` (now also shows open follow-ups).
  - `forget` falls through to the diary when no fact matches: it removes sentences or follow-ups by words,
    or a whole day by date.
  - `sommus diary [day|words]`, `sommus diary forget …`, and `sommus diary write`, which writes the last
    24 hours now for about 1¢.
  - The file itself can be edited.

## Consequences
- Cost: one Haiku call a night with at most ~3.5k tokens of transcript plus ~600 of instructions, and at
  most ~300 tokens out, so ≤ 0.6¢ a night and ≤ ~18¢ a month. Recent-day tokens add ~0.01¢ per request as
  cache reads.
- Memory forms overnight, not live. Something said this morning isn't in the diary until tomorrow. It is still
  in the conversation history meanwhile, and `remember` still saves facts immediately.
- Keyword recall misses paraphrases ("the job thing" won't find "interview"). If that turns out to matter,
  Gemini's free embeddings could be added later without changing the storage.
- If a night's call fails, it isn't retried that night. The next night's run covers both days under one date.
