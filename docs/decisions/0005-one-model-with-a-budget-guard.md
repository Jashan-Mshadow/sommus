# 5. One model plus a monthly budget guard, instead of per-request routing

**Status:** accepted, 2026-09-19

## Context
The obvious cost idea is a router: send each simple request to the cheapest model that can do it.

## Decision
Don't route per request. Keep one model (Sonnet 5, low effort) and add a monthly guard: a once-a-day
notice from 80% of the $10 cap, and Haiku 4.5 from 90% until the 1st.

## Why
- **Prompt caches are per model.** 89% of Sommus's prompt tokens are cache reads at 0.1× price.
  Switching models writes a fresh ~6k-token cache entry at the 1-hour rate (1.2–2.4¢), which is more
  than Haiku saves on a command that costs about 0.7¢ on Sonnet.
- Sonnet already runs at the lowest effort, and the biggest savings came from not calling a model at
  all: the fast path, free data APIs (weather, time, holidays) and the campus engine.
- Haiku doesn't accept the effort setting or adaptive thinking; requests to it drop both.

## Consequences
- Simpler: one cache stays warm, one model's behaviour to test.
- Near the cap, Sommus keeps working (cheaper and slightly less accurate) instead of hitting the
  hard limit mid-month.
