"""Decision-gate telemetry: an explicit, auditable checklist of the same conditions the policy engine's
stop rule already evaluates (agent/policy.py: CASE_GATE_P, STOP_HIGH_P/STOP_LOW_P, the >=2 independent-evidence
corroboration requirement), surfaced as pass/fail line items instead of leaving them implicit in `stop_reason`
prose. This adds no new detection logic and does not change any verdict, action or SAR decision -- it is a
read-only view onto the Assessment the investigation already produced (inv.assessment), computed for the UI
the same way agent/counterfactual.py explains the same investigation from a different angle.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .investigator import Investigation
from .policy import CASE_GATE_P, STOP_HIGH_P, STOP_LOW_P, Assessment

CORROBORATION_MIN = 2


@dataclass
class GateCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class DecisionGate:
    fraud_probability: float
    verdict: str
    evidence_families: int
    corroboration_min: int
    case_gate_open: bool
    stop_reached: bool
    checks: list[GateCheck] = field(default_factory=list)


def evaluate(inv: Investigation) -> DecisionGate | None:
    a: Assessment | None = getattr(inv, "assessment", None)
    if a is None:
        return None
    p = a.fraud_probability
    verdict = "fraud" if p >= STOP_HIGH_P else "legitimate" if p <= STOP_LOW_P else "uncertain"
    families = a.n_independent_evidence
    corroborated = families >= CORROBORATION_MIN
    case_gate_open = p >= CASE_GATE_P
    at_extreme = p >= STOP_HIGH_P or p <= STOP_LOW_P
    stop_reached = at_extreme and corroborated

    checks = [
        GateCheck("Case gate", case_gate_open,
                   f"Fraud probability {p:.2f} {'meets' if case_gate_open else 'is below'} the case-opening bar "
                   f"(>= {CASE_GATE_P:.2f}, policy 3a)."),
        GateCheck("Corroboration", corroborated,
                   f"{families} independent evidence {'family' if families == 1 else 'families'} observed "
                   f"({'>=' if corroborated else '<'} {CORROBORATION_MIN} required)."),
        GateCheck("Belief stability", not a.evidence_conflict,
                   "No conflict between the customer's own report and the graph evidence." if not a.evidence_conflict
                   else "The customer's report conflicts with the graph evidence; escalated instead of guessing."),
        GateCheck("Stop threshold", stop_reached,
                   (f"p={p:.2f} is past the stop band (<= {STOP_LOW_P:.2f} or >= {STOP_HIGH_P:.2f}, policy section 6)"
                    + (" and corroboration is met." if corroborated else "; waiting on corroboration."))
                   if at_extreme else
                   f"p={p:.2f} has not reached the stop band (<= {STOP_LOW_P:.2f} or >= {STOP_HIGH_P:.2f})."),
    ]
    return DecisionGate(fraud_probability=round(p, 3), verdict=verdict, evidence_families=families,
                        corroboration_min=CORROBORATION_MIN, case_gate_open=case_gate_open,
                        stop_reached=stop_reached, checks=checks)
