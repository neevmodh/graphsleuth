"""Case orchestrator: trigger -> investigate -> assess -> initial actions -> (simulated) evidence -> final actions
-> explain -> memory. Returns a validated `Answer`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import explain, narrative
from .backend import GraphBackend
from .episode import EP_DEV_PATH, EP_PATH, EpisodeModel
from .investigator import Cfg, Investigation, Investigator, Step
from .llm import LLMRouter
from .memory import CaseMemory
from .policy import Assessment, recommend
from .schemas import Answer, Case, Evidence, EvidenceRequest, NextBestActions, Sar

# Ambiguity band: inside it the agent does not pretend to know the customer's reply.
UNCERTAIN_LO, UNCERTAIN_HI = 0.35, 0.65


@dataclass
class Simulated:
    request: EvidenceRequest | None
    response: str | None          # denied | confirmed | no_reply | None
    evidence: Evidence | None


def simulate_evidence(inv: Investigation, initial, verdict: str) -> Simulated:
    """Customer/analyst replies are not provided, so the reply is simulated and the assumption recorded.
    The assumed reply follows the agent's own belief; inside the ambiguity band no reply is assumed (R4)."""
    acts = {a.action for a in initial}
    if inv.assessment is not None and inv.assessment.evidence_conflict:
        text = ("No reply is presumed: the customer's report conflicts with the graph evidence, so the case is escalated "
                "for analyst review instead of guessing the outcome")
        return Simulated(EvidenceRequest(type="customer_validation", asked_after_step=len(inv.steps), assumed_response=text), None,
                         Evidence(claim=f"Customer report conflicts with the graph evidence (model support {inv.signals.get('p_stage1', 0):.2f}): {text}",
                                  source="customer", ref="evidence_request:1", entity_ids=[str(inv.trigger['flagged_txn_id'])]))
    kind = "customer_validation" if "VERIFY_WITH_CUSTOMER" in acts else "step_up_auth" if "STEP_UP_AUTH" in acts else None
    if kind is None:
        return Simulated(None, None, None)
    step = len(inv.steps)
    tid = str(inv.trigger["flagged_txn_id"])
    rec = inv.signals.get("recurring", {}).get("is_recurring")
    if verdict == "uncertain":
        resp, text = "no_reply", "No reply within 24 hours (assumption: the evidence is too ambiguous to presume the customer's answer)"
    elif verdict == "fraud":
        resp = "denied"
        text = ("Customer states they did not make the flagged transaction(s) and still holds the card" if kind == "customer_validation"
                else "Step-up authentication is not completed (assumed: the actor is not the cardholder)")
    else:
        resp = "confirmed"
        text = ("Customer recognises the recurring charge when reminded of it" if rec else
                "Customer confirms the purchase, e.g. on a new phone or while travelling (assumption: the evidence points to legitimate use)"
                ) if kind == "customer_validation" else "Step-up authentication is passed (assumed: the actor is the cardholder)"
    ev = Evidence(claim=f"{'Customer' if resp != 'no_reply' else 'No customer'} response (assumed): {text}", source="customer",
                  ref="evidence_request:1", entity_ids=[tid])
    return Simulated(EvidenceRequest(type=kind, asked_after_step=step, assumed_response=text), resp, ev)


class Orchestrator:
    def __init__(self, backend: GraphBackend, memory: CaseMemory | None = None, cfg: Cfg | None = None, llm: LLMRouter | None = None, rag=None):
        cfg = cfg or Cfg(fraud_p=UNCERTAIN_HI, legit_p=UNCERTAIN_LO)
        self.backend, self.memory, self.llm = backend, memory, llm
        self.last: Investigation | None = None
        path = EP_DEV_PATH if getattr(backend, "variant", "final") == "dev" else EP_PATH
        self.inv = Investigator(backend, cfg, EpisodeModel(path) if path.exists() else None, rag=rag)

    def run_case(self, trig: dict, on_step: Callable[[Step], None] | None = None, write_memory: bool = True) -> Answer:
        t0 = time.perf_counter()
        tok0 = self.llm.snapshot() if self.llm else 0
        trig = {**trig, "opened_at": str(trig["opened_at"])}
        inv = self.inv.investigate(trig, on_step)
        self.last = inv                  # kept for the UI (window, episode scores)
        a: Assessment = inv.assessment
        p = a.fraud_probability
        verdict = "fraud" if p >= UNCERTAIN_HI else "legitimate" if p <= UNCERTAIN_LO else "uncertain"
        if a.evidence_conflict:          # the customer says fraud, the graph says normal: neither side settles it
            verdict = "uncertain"

        initial = recommend(a, None)
        sim = simulate_evidence(inv, initial, verdict)
        final = recommend(a, sim.response) if sim.response else initial

        # a confirmed reply settles the case as legitimate: no affected transactions, no exposure
        if sim.response == "confirmed":
            verdict = "legitimate"
        if verdict == "legitimate":
            inv.episode, inv.exposure, inv.first_suspicious, inv.pattern, inv.pattern_description = [], 0.0, "", "none", ""
            inv.connected_cards, inv.connected_devices, inv.span = [], [], None

        final_names = {x.action for x in final}
        status = ("escalated" if "ESCALATE_TO_ANALYST" in final_names else
                  "closed_fraud" if verdict == "fraud" else "closed_legitimate" if verdict == "legitimate" else "open")
        evidence = list(inv.evidence) + ([sim.evidence] if sim.evidence else [])

        sar_file = "FILE_REPORT" in final_names
        if sar_file:
            sar_narr = explain.polish(self.llm, narrative.sar_narrative(inv), "sar").text
            if inv.rag is not None and self.llm is not None:                      # cite the FinCEN/AML guidance this report follows
                g = explain.sar_grounding(self.llm, sar_narr, inv.rag.pack(sources={"regulatory"}))
                if g.used_llm:
                    sar_narr = f"{sar_narr} {g.text}"
            sar = Sar(file=True, reason=next(x.reason for x in final if x.action == "FILE_REPORT"),
                      narrative=sar_narr, subjects=narrative.sar_subjects(inv),
                      total_amount_usd=inv.exposure, activity_dates=[inv.span[0][:10], inv.span[1][:10]])
        else:
            sar = Sar(file=False, reason=_no_report_reason(inv, a, verdict))

        graph_case_id, written = "", False
        similar_ids = [c["case_id"] for c in inv.similar if c.get("score", 0) >= 2][:3]
        if inv.rag is not None:                        # add real example cases of the best-matching typology (same outcome as our verdict)
            for h in inv.rag.by_source("typology")[:1]:
                if h.score >= 0.5 and (("confirmed fraud" in h.title) == (verdict != "legitimate")):
                    similar_ids += [e["case_id"] for e in h.examples if e["case_id"] not in similar_ids][:2]
        summary_text = explain.polish(self.llm, narrative.summary(inv, verdict, p, sim.response), "summary").text
        if inv.rag is not None and self.llm is not None:
            facts = (f"verdict {verdict}, fraud probability {p:.2f}, pattern {inv.pattern}, exposure ${inv.exposure:,.2f}; "
                     f"final actions: " + "; ".join(f"{x.action} ({x.reason})" for x in final))
            r = explain.rationale(self.llm, facts, inv.rag.pack())
            if r.used_llm:
                summary_text = f"{summary_text} Rationale: {r.text}"
        case = Case(
            status=status, verdict=verdict, fraud_probability=round(p, 3), pattern=inv.pattern,
            pattern_description=inv.pattern_description if inv.pattern == "undocumented" else "",
            affected_txn_ids=[str(t) for t in inv.episode], first_suspicious_txn_id=inv.first_suspicious,
            connected_card_ids=inv.connected_cards, connected_device_profiles=inv.connected_devices,
            exposure_usd=inv.exposure, evidence=evidence, similar_prior_cases=similar_ids,
            summary=summary_text)
        if write_memory and self.memory is not None:
            graph_case_id, written = self.memory.write(trig, case, inv, final)
            case.written_to_graph, case.graph_case_id = written, graph_case_id

        nba = NextBestActions(initial=initial, final=final, what_changed=_what_changed(initial, final, sim.response))
        return Answer(
            case_id=trig["case_id"], case=case, evidence_requests=[sim.request] if sim.request else [],
            next_best_actions=nba, sar=sar, stop_reason=_stop_reason(inv, a, sim, verdict),
            tool_calls=len(inv.steps), tokens=(self.llm.snapshot() - tok0) if self.llm else 0, latency_s=round(time.perf_counter() - t0, 2))


def _what_changed(initial, final, response) -> str:
    if [(x.action, x.route) for x in initial] == [(x.action, x.route) for x in final]:
        return "nothing"
    ini, fin = ", ".join(x.action for x in initial), ", ".join(x.action for x in final)
    why = {"denied": "the assumed customer denial", "confirmed": "the assumed customer confirmation",
           "no_reply": "the assumed absence of a reply within 24 hours"}.get(response, "new evidence")
    return f"After {why}, the recommendation moved from [{ini}] to [{fin}]."


def _no_report_reason(inv: Investigation, a: Assessment, verdict: str) -> str:
    if verdict == "legitimate":
        return "3a: no fraud confirmed or strongly suspected, so no suspicious activity report is required."
    if verdict == "uncertain":
        return "3a: fraud is not yet confirmed or strongly suspected; escalate for analyst review instead of reporting."
    return (f"3a: fraud is likely but exposure (${inv.exposure:,.2f}) is at or below $1,000, there is no shared device, "
            f"region or other-customer link, and the pattern is documented: case only, no report.")


def _stop_reason(inv: Investigation, a: Assessment, sim: Simulated, verdict: str) -> str:
    if inv.signals.get("ring", {}).get("hit"):
        return (f"A shared-device ring across {len(inv.connected_cards) + 1} cards was identified and the affected cards are named; "
                f"further queries would not change the actions (R6/R9).")
    if sim.response in ("denied", "confirmed"):
        return "The assumed verification reply settled the question; further steps are unlikely to change the decision."
    if sim.response == "no_reply":
        return "Evidence remains ambiguous after the graph and history checks and no reply is assumed; the recommendation is monitoring and escalation rather than a forced verdict."
    if a.fraud_probability >= 0.85 and a.n_independent_evidence >= 2:
        return f"Fraud probability {a.fraud_probability:.2f} with {a.n_independent_evidence} independent evidence items reached the stop threshold (section 6)."
    return "Further steps are unlikely to change the decision."
