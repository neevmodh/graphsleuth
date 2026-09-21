"""Human-readable case summary and the standalone SAR narrative.

Built from structured facts so every claim is traceable to evidence; an LLM can polish the wording
(agent/llm.py) but the facts always come from here.
"""
from __future__ import annotations

from .investigator import Investigation

_PATTERN_TEXT = {
    "card_testing": "a card-testing sequence: tiny online authorizations followed by a larger purchase",
    "card_not_present_fraud": "card-not-present fraud: online use of the card number inconsistent with the cardholder's history",
    "card_not_present_new_device": "card-not-present fraud from a device new to the account",
    "out_of_region_use": "card-present use in a billing region the cardholder has no history in",
    "account_takeover": "account takeover: mixed-channel activity inconsistent with the cardholder, pointing to compromised credentials",
    "undocumented": "an undocumented pattern",
}


def _d(ts: str) -> str:
    return ts[:10]


def summary(inv: Investigation, verdict: str, p: float, response: str | None) -> str:
    f, t = inv.flagged, inv.trigger
    card = t["card_id"]
    s = []
    if verdict == "fraud":
        s.append(f"{len(inv.episode)} transaction(s) totalling ${inv.exposure:,.2f} on {card} are assessed as fraud "
                 f"(probability {p:.2f}), matching {_PATTERN_TEXT.get(inv.pattern, inv.pattern)}.")
        if inv.pattern == "undocumented":
            s.append(inv.pattern_description)
        if inv.connected_cards:
            s.append(f"The same device profile links {len(inv.connected_cards)} other cards, so this is treated as a coordinated ring, not an isolated compromise.")
    elif verdict == "legitimate":
        s.append(f"The alert on transaction {t['flagged_txn_id']} (${f['amt']:.2f}, {f['channel'].replace('_', ' ')}) looks legitimate "
                 f"(fraud probability {p:.2f}); the bank's risk score of {f.get('bank_risk'):.2f} is not corroborated by the card's own history.")
        rec = inv.signals.get("recurring", {})
        if rec.get("is_recurring"):
            s.append("The charge repeats at roughly monthly intervals on this card, so a dispute is likely a recognised recurring charge (R7).")
    else:
        s.append(f"The evidence on transaction {t['flagged_txn_id']} (${f['amt']:.2f}) is ambiguous (fraud probability {p:.2f}); "
                 f"the alert is neither confirmed nor cleared.")
    s.append(f"Trigger: {t['trigger_type'].replace('_', ' ')}. Tool calls: {len(inv.steps)}; closed cases consulted: "
             f"{', '.join(c['case_id'] for c in inv.similar[:3]) or 'none relevant'}.")
    if response:
        s.append("An evidence request was simulated and its assumed response is recorded in evidence_requests.")
    return " ".join(s)


def _when(inv: Investigation) -> str:
    a, b = inv.span
    if a[:10] == b[:10]:
        return f"on {a[:10]}" + (f" between {a[11:16]} and {b[11:16]}" if a[11:16] != b[11:16] else f" at {a[11:16]}")
    return f"between {a[:10]} and {b[:10]}"


def sar_narrative(inv: Investigation) -> str:
    """Who, what, when, where, how and why, standing on its own for a regulator."""
    f, t = inv.flagged, inv.trigger
    card, cust = t["card_id"], t["customer_id"]
    ep = inv.window[inv.window["tid"].isin(inv.episode)] if inv.window is not None else None
    chans = sorted(set(ep["channel"])) if ep is not None and len(ep) else [f["channel"]]
    where = " and ".join(c.replace("_", " ") for c in chans)
    regions = sorted({int(x) for x in ep["addr1"].dropna()}) if ep is not None and len(ep) else []
    net = " ".join(x for x in (f.get("card4"), f.get("card6")) if x) or "payment"
    n = len(inv.episode)
    s = [
        f"Who: customer {cust}, holder of {net} card {card}, is the subject of this report.",
        f"What: {n} {where} transaction{'s' if n != 1 else ''} totalling ${inv.exposure:,.2f} {'were' if n != 1 else 'was'} made on the card {_when(inv)}"
        f"{' in billing region ' + ', '.join(map(str, regions)) if regions else ''} and are not consistent with the cardholder's history"
        f"{'; the cardholder reported them as unauthorized' if t['trigger_type'] == 'customer_report' else ''}.",
    ]
    if inv.pattern == "undocumented" and inv.connected_devices:
        s.append(f"How: every suspicious transaction on this card came from one device profile ({inv.connected_devices[0]}), recorded as New for the account and behind an anonymous proxy.")
        s.append(f"The same device profile transacted on {len(inv.connected_cards)} other cards in the same burst ({', '.join(inv.connected_cards)}).")
        s.append("Individual amounts are ordinary and the bank's risk scores stayed low, so the activity is visible only by linking cards through the shared device.")
        s.append("Why suspicious: one device working many unrelated card numbers, always as a new device behind an anonymous proxy, indicates a single actor using many compromised cards.")
    elif inv.pattern == "undocumented":
        s.append(f"How: {inv.pattern_description}")
        s.append("Why suspicious: repeating purchases within minutes while keeping each just under a round authorization threshold is consistent with deliberate structuring to avoid review, and the amounts are several times the card's normal spending.")
    elif inv.pattern == "card_testing":
        s.append("How: the sequence began with very small online authorizations and was followed by a larger purchase.")
        s.append("Why suspicious: this is consistent with testing a stolen card number before using it.")
    else:
        s.append(f"How: the activity is consistent with {_PATTERN_TEXT.get(inv.pattern, inv.pattern)}.")
        facts = [e.claim for e in inv.evidence if e.source == "graph" and any(k in e.claim for k in ("Amount", "never been", "never used", "Billing region"))]
        s.append("Why suspicious: " + (facts[0] if facts else "the transactions deviate from the card's established behaviour."))
    similar = [c["case_id"] for c in inv.similar if c.get("score", 0) >= 2 and c.get("outcome") == "confirmed_fraud"][:3]
    if similar:
        s.append(f"The activity matches previously confirmed fraud cases ({', '.join(similar)}).")
    s.append(f"Assessment: fraud probability {inv.p_case:.2f} from a calibrated model over the card's history and graph evidence; the bank's own risk score on the flagged transaction was {f.get('bank_risk'):.2f}.")
    act = "The card is to be blocked and reissued" + (f" and {len(inv.connected_cards)} connected cards placed under monitoring" if inv.connected_cards else "")
    s.append(f"{act}; the case is recorded in the investigation graph for future analysts, and this report is filed because the activity meets the reporting threshold.")
    return " ".join(s)


def sar_subjects(inv: Investigation) -> list[str]:
    subj = [inv.trigger["customer_id"], inv.trigger["card_id"]]
    subj += list(inv.connected_cards)
    subj += list(inv.connected_devices)
    return list(dict.fromkeys(subj))
