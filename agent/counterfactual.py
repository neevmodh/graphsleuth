"""Counterfactual explanations (#27): given the same base model score, what would the case-level fraud probability
have been if one evidence signal had come out differently, and would that have crossed a policy decision threshold
(legitimate <= 0.15, a case opens >= 0.30, fraud confirmed >= 0.85)?

This recomputes the SAME deterministic adjustment agent/investigator.py applies after the model score (the
"probability" section of Investigator.investigate) with exactly one signal flipped, so every counterfactual is the
actual decision rule run again -- not an approximation, and not a second model. It intentionally does not re-run the
stage-2 episode model itself (that would need re-featurising the window under a hypothetical world where, say, the
device had been seen before, which the model was never trained to answer); the counterfactual is scoped to the
rule-based adjustments on top of the model score, which is exactly where R1/R5/R6/R7's discretion actually lives.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .investigator import Cfg, Investigation, _logit, _sigmoid
from .policy import CASE_GATE_P, STOP_HIGH_P, STOP_LOW_P

_THRESHOLDS = [("legitimate (<=0.15)", STOP_LOW_P), ("a case opens (>=0.30)", CASE_GATE_P), ("fraud confirmed (>=0.85)", STOP_HIGH_P)]

_LABELS = {
    "trigger_is_report": "the alert had not been a customer report",
    "testing": "the card-testing sequence (R5) had not been flagged",
    "structuring": "the threshold-structuring burst had not been flagged",
    "ring": "the shared-device ring (R6) had not been flagged",
    "recurring": "the charge had not matched the customer's recurring pattern (R7)",
}


@dataclass
class Counterfactual:
    signal: str
    description: str
    p_before: float
    p_after: float
    crosses: list[str] = field(default_factory=list)


def _apply(p0: float, sig: dict, cfg: Cfg) -> float:
    """The exact formula in Investigator.investigate's 'probability' section, replayed with `sig` in place of the
    real signals dict."""
    p = p0
    if sig["trigger_is_report"]:
        p = _sigmoid(_logit(p) + cfg.report_logit)
    if sig["testing"]:
        p = max(p, 0.88)
    if sig["structuring"]:
        p = max(p, 0.90)
    if sig["ring"]:
        p = max(p, 0.92)
    if sig["recurring"] and not (sig["testing"] or sig["structuring"] or sig["ring"]):
        p *= 0.4
    return min(max(p, 0.01), 0.99)


def _crossed(p0: float, p1: float) -> list[str]:
    return [name for name, t in _THRESHOLDS if (p0 - t) * (p1 - t) < 0]


def explain(inv: Investigation, cfg: Cfg | None = None) -> list[Counterfactual]:
    """One counterfactual per evidence signal that actually fired in this case, flipping only that signal off and
    replaying the probability formula from the same base model score."""
    cfg = cfg or Cfg()
    sig = inv.signals
    base = {
        "trigger_is_report": inv.trigger["trigger_type"] == "customer_report",
        "testing": bool(sig.get("testing", {}).get("hit") and sig["testing"].get("flagged_in_run")),
        "structuring": bool(sig.get("structuring", {}).get("hit") and sig["structuring"].get("flagged_in_run")),
        "ring": bool(sig.get("ring", {}).get("hit")),
        "recurring": bool(sig.get("recurring", {}).get("is_recurring")),
    }
    p0_model = sig.get("p_model", inv.p_flagged)
    out = []
    for key, present in base.items():
        if not present:
            continue                          # only counterfactuals for signals that actually fired this case
        p1 = _apply(p0_model, {**base, key: False}, cfg)
        if abs(p1 - inv.p_case) < 0.005:
            continue                          # no real effect (e.g. another floor already dominates)
        cross = _crossed(inv.p_case, p1)
        tail = f" This crosses the {', '.join(cross)} threshold." if cross else " This does not change the verdict."
        out.append(Counterfactual(signal=key, p_before=round(inv.p_case, 3), p_after=round(p1, 3), crosses=cross,
                                  description=f"If {_LABELS[key]}, the fraud probability would be {p1:.2f} instead of {inv.p_case:.2f}.{tail}"))
    return out
