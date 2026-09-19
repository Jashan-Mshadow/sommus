# 3. Sonnet 5 at low effort, chosen by measurement

**Status:** accepted, 2026-09-14; re-checked with Haiku 4.5 on 2026-09-19

## Context
Sommus started on Opus 5. At about 20 commands a day the monthly cap is $10, so cost per command
matters as much as accuracy.

## Decision
A scored eval (`evals/commands.toml`, fresh conversation per command, read-only tools real and the
rest simulated) decides the model:

| Model / effort | Score | Per command | Latency |
|---|---|---|---|
| Opus 5 / medium | 37/37 | 2.14¢ | 5.6 s |
| Sonnet 5 / low | 34/37 | 0.63¢ | 3.7 s |
| Sonnet 5 / low, after prompt fixes | 36/37 | 0.66¢ | 3.6 s |
| Haiku 4.5 (2026-09-19, 46-command suite) | 41/46 | 0.26¢ | 1.8 s |

Two of Sonnet's three original misses were Sommus's fault (it knew nothing about me, and the prompt
didn't say to act on the likeliest reading), fixed with a profile and one prompt rule.

Haiku is the cheapest and fastest, but against Sonnet on the same cases it missed "put the laptop to
sleep" (it dimmed the screen and changed the volume) and a web search. Two of its other misses fail on
Sonnet too. So Haiku is the end-of-month cheap mode (decision 5), not the default.

## Consequences
- About 3x cheaper and 1.5 s faster than Opus, with no meaningful accuracy loss on this workload.
- Any model change has to beat or match the eval first.
