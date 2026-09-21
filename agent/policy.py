"""Deterministic fraud-policy engine (README "Fraud Policy" v1.0, rules R1-R10).

The LLM proposes an assessment; this module turns it into exact action names,
approval routes and rule citations, so policy is never hallucinated.
Pure functions, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from .schemas import ActionItem

Response = Optional[Literal["denied", "confirmed", "no_reply"]]

AUTO = {
    "ALLOW_TRANSACTION", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS", "WARN_CUSTOMER",
    "VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH", "GENERATE_REPORT", "CREATE_CASE",
    "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD",
}

# Thresholds straight from the policy text.
CASE_GATE_P = 0.30          # 3a: open a case at fraud probability >= 0.30
WEAK_SIGNAL_P = 0.70        # R1
STOP_HIGH_P, STOP_LOW_P = 0.85, 0.15   # section 6
REPORT_EXPOSURE = 1_000.0   # R2 / 3a
ESCALATE_EXPOSURE = 500.0   # R4 / R8
BLOCK_L2_EXPOSURE = 2_500.0  # section 2

# Order actions by what happens first (policy section 1: "order them by what happens first").
_ORDER = [
    "DECLINE_TRANSACTION", "STEP_UP_AUTH", "VERIFY_WITH_CUSTOMER", "BLOCK_CARD", "BLOCK_ALL_CARDS",
    "WARN_CUSTOMER", "CREATE_CASE", "ESCALATE_TO_ANALYST", "MONITOR_CARD",
    "MONITOR_CONNECTED_CARDS", "FILE_REPORT", "GENERATE_REPORT", "ALLOW_TRANSACTION", "CLOSE_NO_FRAUD",
]


def route_for(action: str, exposure_usd: float = 0.0) -> str:
    """Section 2: approval routing."""
    if action in AUTO:
        return "auto"
    if action == "DECLINE_TRANSACTION":
        return "L1"
    if action == "BLOCK_CARD":
        return "L2" if exposure_usd > BLOCK_L2_EXPOSURE else "L1"
    if action in ("BLOCK_ALL_CARDS", "FILE_REPORT"):
        return "L2"
    raise ValueError(f"unknown action {action}")


@dataclass
class Assessment:
    """What the investigation established. Filled by the agent from graph evidence."""
    fraud_probability: float
    exposure_usd: float = 0.0
    trigger_type: Literal["risk_score", "customer_report", "analyst_request"] = "risk_score"
    n_independent_evidence: int = 1
    evidence_conflict: bool = False
    pattern: str = "none"
    card_testing: bool = False           # R5: >=3 small online auths within 1h then larger purchase
    large_purchase_cleared: bool = False  # R5: a purchase > $100 already cleared
    shared_origin: bool = False          # R6: same device/region/email across cards with fraud
    connected_fraud_cards: list[str] = field(default_factory=list)
    recurring_dispute: bool = False      # R7: dispute matches customer's own monthly recurring charge
    undocumented_coordinated: bool = False  # R9
    confirmed_fraud_cards: int = 0       # R10
    credentials_compromised: bool = False  # R10

    @property
    def verdict(self) -> str:
        p = self.fraud_probability
        if p >= STOP_HIGH_P:
            return "fraud"
        if p <= STOP_LOW_P:
            return "legitimate"
        return "uncertain"


def sar_required(a: Assessment, fraud_strongly_suspected: bool | None = None) -> bool:
    """3a: file only when fraud is confirmed/strongly suspected AND (exposure > $1,000, shared
    device/region/other-customer fraud, or a coordinated/undocumented pattern)."""
    strong = a.fraud_probability >= WEAK_SIGNAL_P if fraud_strongly_suspected is None else fraud_strongly_suspected
    if not strong:
        return False
    return (
        a.exposure_usd > REPORT_EXPOSURE
        or a.shared_origin
        or bool(a.connected_fraud_cards)
        or a.pattern == "undocumented"
        or a.undocumented_coordinated
    )


def stop_reached(a: Assessment, response: Response = None) -> bool:
    """Section 6 stop rule (the 'further steps won't help' clause is decided by the agent)."""
    if response in ("denied", "confirmed"):
        return True
    p = a.fraud_probability
    return (p >= STOP_HIGH_P or p <= STOP_LOW_P) and a.n_independent_evidence >= 2


def needs_evidence(a: Assessment) -> bool:
    """Should the agent request more evidence (verify / step-up) before acting?"""
    return not stop_reached(a) and not (a.trigger_type == "customer_report" and not a.recurring_dispute)


def _item(action: str, reason: str, exposure: float) -> ActionItem:
    return ActionItem(action=action, route=route_for(action, exposure), reason=reason)


def recommend(a: Assessment, response: Response = None) -> list[ActionItem]:
    """Map an assessment (+ optional simulated customer response) to policy actions.

    `response=None` gives the *initial* recommendation, before any requested evidence
    comes back; passing the assumed response gives the *final* one.
    """
    acts: dict[str, str] = {}
    p, exp = a.fraud_probability, a.exposure_usd

    def add(action: str, reason: str) -> None:
        acts.setdefault(action, reason)

    def fraud_path(basis: str) -> None:
        add("BLOCK_CARD", basis)
        add("CREATE_CASE", basis)
        if sar_required(a, fraud_strongly_suspected=True):
            add("FILE_REPORT", "R2/R6: exposure, shared origin or coordinated pattern meets the report bar")
        if a.shared_origin or a.connected_fraud_cards:
            add("MONITOR_CONNECTED_CARDS", "R6: monitor every card sharing the origin")

    if response == "confirmed":                                   # R3
        add("CLOSE_NO_FRAUD", "R3: customer confirmed the transaction")
    elif response == "denied":                                    # R2
        fraud_path("R2: customer denies the transaction")
    elif response == "no_reply":                                  # R4
        add("MONITOR_CARD", "R4: no reply within 24h")
        add("DECLINE_TRANSACTION", "R4: decline pending authorizations")
        add("CREATE_CASE", "3a: evidence was requested")
        if exp > ESCALATE_EXPOSURE:
            add("ESCALATE_TO_ANALYST", "R4: no reply and exposure > $500")
    elif a.recurring_dispute:                                     # R7
        add("CREATE_CASE", "R7: disputed charge matches the customer's recurring pattern")
        add("VERIFY_WITH_CUSTOMER", "R7: confirm before any action; do not block")
        add("WARN_CUSTOMER", "R7: remind the customer of the recurring charge")
    elif a.trigger_type == "customer_report":                     # the report is itself a denial (R2)
        fraud_path("R2: customer reported the transaction as unauthorized")
    elif a.card_testing:                                          # R5
        if a.large_purchase_cleared:
            fraud_path("R5: card testing and a purchase > $100 already cleared")
        else:
            add("DECLINE_TRANSACTION", "R5: testing sequence followed by a larger purchase")
            add("STEP_UP_AUTH", "R5: require step-up before further activity")
            add("CREATE_CASE", "3a: evidence was requested")
    elif p >= STOP_HIGH_P and a.n_independent_evidence >= 2:      # strong, multi-evidence
        fraud_path("Stop rule: p >= 0.85 with >= 2 independent evidence items")
    elif p <= STOP_LOW_P and a.n_independent_evidence >= 2:
        add("CLOSE_NO_FRAUD", "Stop rule: p <= 0.15 with >= 2 independent evidence items")
    else:                                                         # ambiguous: verify first
        if p < WEAK_SIGNAL_P or a.n_independent_evidence < 2:
            add("VERIFY_WITH_CUSTOMER", "R1: single/weak signal (p < 0.70): verify before any block")
            add("CREATE_CASE", "3a: evidence was requested")
        else:
            fraud_path("p >= 0.70 with multiple independent signals")
        if a.verdict == "uncertain" and (exp > ESCALATE_EXPOSURE or a.evidence_conflict):   # R8
            add("ESCALATE_TO_ANALYST", "R8: uncertain and exposed (> $500) or evidence conflicts")

    if a.undocumented_coordinated or a.pattern == "undocumented":  # R9
        add("CREATE_CASE", "R9: undocumented coordinated pattern")
        if p >= WEAK_SIGNAL_P:
            add("FILE_REPORT", "R9: undocumented pattern across customers")
        add("ESCALATE_TO_ANALYST", "R9: describe the pattern and escalate")

    # R10: never BLOCK_ALL_CARDS unless >=2 cards confirmed fraud or credentials compromised.
    if "BLOCK_ALL_CARDS" in acts and not (a.confirmed_fraud_cards >= 2 or a.credentials_compromised):
        del acts["BLOCK_ALL_CARDS"]

    # FILE_REPORT must have a case behind it (3a).
    if "FILE_REPORT" in acts:
        acts.setdefault("CREATE_CASE", "3a: a report always has a case behind it")

    ordered = sorted(acts, key=_ORDER.index)
    return [_item(x, acts[x], exp) for x in ordered]
