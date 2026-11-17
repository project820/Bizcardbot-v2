"""Token pricing and value-free, per-card review statistics."""

from __future__ import annotations

import math
import os

from . import cards
from .draft import CardDraft, EDITABLE_FIELDS

# USD per million tokens; keep in sync with the private recognition benchmark.
PRICES = {
    "gpt-6-luna": {"input": .10, "cached": .01, "write": .125, "output": .50, "context": 272000},
    "gpt-5.6-luna": {"input": .20, "cached": .02, "write": .25, "output": 1.20, "context": 272000},
    "claude-haiku-5-5": {"input": .10, "cached": .01, "write": .125, "output": .50, "context": 100000},
}


def cost_krw(provider: str, model: str, usage: dict) -> float | None:
    """Apply the benchmark's cache-aware pricing; unknown usage stays unknown."""
    price = PRICES.get(model)
    if price is None or not usage or "input_tokens" not in usage or "output_tokens" not in usage:
        return None
    try:
        inp, out = int(usage["input_tokens"]), int(usage["output_tokens"])
        if inp < 0 or out < 0 or inp > price["context"]:
            return None
        if provider == "openai":
            details = usage.get("input_tokens_details") or {}
            cached = int(details.get("cached_tokens") or 0)
            write = int(details.get("cache_write_tokens", details.get("cache_creation_tokens", 0)) or 0)
            longwrite = int((details.get("cache_write_tokens_details") or {}).get("ttl_12h_tokens") or 0)
            value = (max(0, inp - cached - write) * price["input"] + cached * price["cached"]
                     + max(0, write - longwrite) * price["write"] + longwrite * price["input"] * 2.5)
        elif provider == "anthropic":
            cached = int(usage.get("cache_read_input_tokens") or 0)
            write = int(usage.get("cache_creation_input_tokens") or 0)
            hour = int((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
            value = (inp * price["input"] + cached * price["cached"]
                     + max(0, write - hour) * price["write"] + hour * price["input"] * 2)
        else:
            return None
        if min(cached, write, longwrite if provider == "openai" else hour) < 0:
            return None
    except (TypeError, ValueError, OverflowError, AttributeError):
        return None
    try:
        fx = float(os.environ.get("BIZCARD_USD_KRW", "1335"))
        if not math.isfinite(fx) or fx <= 0:
            fx = 1335
    except ValueError:
        fx = 1335
    return (value + out * price["output"]) / 1e6 * fx


def _review(card: dict) -> dict:
    review = card.setdefault("review", {})
    for key, default in (("edited", []), ("ai_edits", 0), ("manual_edits", 0),
                         ("rerecognized", False), ("back_added", False), ("warnings", 0), ("agree", {})):
        review.setdefault(key, default)
    return review


def record_edit(card: dict, before: CardDraft, after: CardDraft, event: str) -> None:
    """Record field names and counts only, including fields later reverted."""
    review = _review(card)
    if event in ("manual_edit", "ai_edit"):
        changed = {key for key in EDITABLE_FIELDS if getattr(before, key) != getattr(after, key)}
        review["edited"] = sorted(set(review["edited"]) | changed)
        key = "manual_edits" if event == "manual_edit" else "ai_edits"
        review[key] += 1
    if event == "rerecognized" or (event == "recognized" and
            (any(getattr(before, key) for key in EDITABLE_FIELDS) or before.warnings)):
        review["rerecognized"] = True
    if event in ("back_added", "back_merged"):
        review["back_added"] = True
    review["warnings"] = len(after.warnings)


def record_calls(card: dict, calls: list[dict]) -> float | None:
    """Accumulate measured cost/count; return this collection's cost only.

    Any unknown call makes the cumulative total unknown. Empty collections do
    not create cost state (older cards and non-AI edits remain unmeasured).
    """
    if not calls:
        return None
    costs = [call.get("krw") for call in calls]
    current = sum(costs) if all(_known_cost(cost) for cost in costs) else None
    previous = card.get("cost")
    total = current
    if previous is not None:
        old = previous.get("krw")
        total = old + current if _known_cost(old) and current is not None else None
    card["cost"] = {"krw": total, "calls": (previous or {}).get("calls", 0) + len(calls)}
    for call in calls:
        if isinstance(call.get("agree"), dict):
            agree = _review(card)["agree"]
            for key, value in call["agree"].items():
                if key in EDITABLE_FIELDS and isinstance(value, bool):
                    # An earlier disagreement stays visible across recognition retries.
                    agree[key] = agree.get(key, True) and value
    return current


def _known_cost(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def summary(chat_id) -> dict:
    saved = cards.list_cards(chat_id, ["saved"])
    reviews = [card["review"] for card in saved if isinstance(card.get("review"), dict)]
    unedited = sum(not review.get("edited") and not review.get("ai_edits")
                   and not review.get("manual_edits") and not review.get("rerecognized")
                   for review in reviews)
    costs = [(card.get("cost") or {}).get("krw") for card in saved]
    known = [cost for cost in costs if _known_cost(cost)]
    return {"saved": len(saved), "unedited_rate": unedited / len(reviews) if reviews else None,
            "avg_krw": sum(known) / len(known) if known else None,
            "reviewed": len(reviews), "costed": len(known)}
