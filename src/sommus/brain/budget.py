"""Monthly spend guard and model choice.

Why not route every request to the cheapest model that could handle it? Measured and read up
(2026-09-19): prompt caches are per model, so a request that switches models writes a fresh cache
entry (~6k tokens at the 1-hour rate, ~1.2–2.4¢) — more than Haiku saves on a ~0.7¢ command. And
Sonnet 5 already runs at the lowest effort. So Sommus stays on one model and only steps down when
the month's budget is nearly gone, where finishing the month matters more than a warm cache.
"""

from __future__ import annotations

from dataclasses import dataclass

# Haiku 4.5 predates the request options Sommus uses on Sonnet 5: effort (a 400 on Haiku) and
# adaptive thinking (Haiku takes a fixed budget instead). Requests to it drop both.
NO_EFFORT = {"claude-haiku-4-5"}
NO_ADAPTIVE_THINKING = {"claude-haiku-4-5"}


@dataclass(frozen=True)
class Budget:
    monthly_usd: float = 10.0
    warn_at: float = 0.8  # tell Jashan once a day from here
    cheap_at: float = 0.9  # switch to the cheap model from here
    cheap_model: str = "claude-haiku-4-5"

    @classmethod
    def from_section(cls, raw: dict) -> Budget:
        return cls(
            monthly_usd=float(raw.get("monthly_usd", 10.0)),
            warn_at=float(raw.get("warn_at", 0.8)),
            cheap_at=float(raw.get("cheap_at", 0.9)),
            cheap_model=raw.get("cheap_model", "claude-haiku-4-5"),
        )


@dataclass(frozen=True)
class Decision:
    model: str
    spent: float
    notice: str = ""  # said once per day, when crossing a threshold matters


def choose(budget: Budget, main_model: str, spent: float) -> Decision:
    share = spent / budget.monthly_usd if budget.monthly_usd > 0 else 0.0
    used = f"${spent:.2f} of ${budget.monthly_usd:.0f} this month"
    if share >= budget.cheap_at:
        return Decision(
            budget.cheap_model,
            spent,
            f"Budget: {used}. Using the cheaper model until the month resets; basic commands are still free.",
        )
    if share >= budget.warn_at:
        return Decision(main_model, spent, f"Budget heads-up: {used}.")
    return Decision(main_model, spent)


def request_options(model: str, effort: str, thinking: dict) -> dict:
    """Only the options this model accepts."""
    options: dict = {}
    if model not in NO_EFFORT:
        options["output_config"] = {"effort": effort}
    if model in NO_ADAPTIVE_THINKING:
        if thinking.get("type") == "disabled":
            options["thinking"] = {"type": "disabled"}
        # adaptive -> leave thinking off: Haiku is the cheap path, and a fixed budget costs tokens
    else:
        options["thinking"] = thinking
    return options
