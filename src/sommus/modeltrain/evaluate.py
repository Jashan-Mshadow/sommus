"""Stage 5: does a candidate model pick the same tool and arguments as Claude did?

A model answers each held-out request with {"calls": [...]} or {"escalate": True}. Scores, per set:
  tool_accuracy   of the requests Claude handled with tools, how often the model named the same tool(s)
  arg_accuracy    ... and the same arguments too
  answer_rate     how often the model acted instead of escalating
  wrong_action    share of all requests where it acted and was wrong (wrong tool/args, or acted when it should
                  have escalated): the dangerous kind
  score           escalation-aware, mean over requests: +1 right action or right escalation, 0 for escalating
                  something it could have done (costs money, harms nothing), -2 for acting wrongly
The baseline "always escalate" is scored the same way, so a model has to beat it to be worth anything.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sommus.modeltrain import common

WRONG_ACTION = -2.0
Predict = Callable[[str], dict]


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in sorted(value.items())}
    return value


def _tools(calls: list[dict]) -> list[str]:
    return [c.get("tool", "") for c in calls]


def _same_args(a: list[dict], b: list[dict]) -> bool:
    return len(a) == len(b) and all(
        _norm(x.get("args") or {}) == _norm(y.get("args") or {}) for x, y in zip(a, b, strict=True)
    )


def score_one(expected: dict, predicted: dict) -> dict:
    escalated = bool(predicted.get("escalate")) or not predicted.get("calls")
    want = expected.get("calls") or []
    acted = not escalated
    got = [] if escalated else predicted["calls"]
    tool_ok = acted and bool(want) and _tools(got) == _tools(want)
    arg_ok = tool_ok and _same_args(got, want)
    if want:
        points = 1.0 if arg_ok else (0.0 if escalated else WRONG_ACTION)
    else:
        points = 1.0 if escalated else WRONG_ACTION
    return {
        "expects_tools": bool(want),
        "acted": acted,
        "tool_ok": tool_ok,
        "arg_ok": arg_ok,
        "wrong": acted and not arg_ok,
        "points": points,
    }


def evaluate(examples: list[dict], predict: Predict) -> dict:
    n = len(examples)
    if not n:
        return {"n": 0}
    rows = [score_one(common.target(ex), predict(ex["request"])) for ex in examples]
    tooled = [r for r in rows if r["expects_tools"]]
    return {
        "n": n,
        "expects_tools": len(tooled),
        "tool_accuracy": sum(r["tool_ok"] for r in tooled) / len(tooled) if tooled else None,
        "arg_accuracy": sum(r["arg_ok"] for r in tooled) / len(tooled) if tooled else None,
        "answer_rate": sum(r["acted"] for r in rows) / n,
        "wrong_action_rate": sum(r["wrong"] for r in rows) / n,
        "score": sum(r["points"] for r in rows) / n,
    }


def always_escalate(_request: str) -> dict:
    return {"escalate": True}


def run_all(real_test: list[dict], synthetic_test: list[dict], model: Predict | None) -> dict:
    """Baseline always; the candidate only when there is one. Real and synthetic are never mixed."""
    out: dict = {"baseline": {"real": evaluate(real_test, always_escalate),
                              "synthetic": evaluate(synthetic_test, always_escalate)}}  # fmt: skip
    out["candidate"] = (
        {"real": evaluate(real_test, model), "synthetic": evaluate(synthetic_test, model)} if model else None
    )
    return out


def load_mlx_model(base_model: str, adapter: str) -> Predict:  # pragma: no cover - needs mlx-lm and a trained adapter
    """The trained model as a predict function. Lazy imports: this machine may not have mlx-lm at all."""
    import json

    from mlx_lm import generate, load

    from sommus.modeltrain.train import SYSTEM_PROMPT

    model, tokenizer = load(base_model, adapter_path=adapter)

    def predict(request: str) -> dict:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": request}]
        prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
        text = generate(model, tokenizer, prompt=prompt, max_tokens=200)
        try:
            found = json.loads(text[text.index("{") : text.rindex("}") + 1])
            return found if isinstance(found, dict) else {"escalate": True}
        except ValueError:
            return {"escalate": True}  # unreadable output is treated as "I don't know"

    return predict
