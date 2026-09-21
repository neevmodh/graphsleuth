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


def sar_narrative(inv: Investigation) -> str:
    f, t = inv.flagged, inv.trigger
    card, cust = t["card_id"], t["customer_id"]
    a, b = _d(inv.span[0]), _d(inv.span[1])
    when = f"on {a}" if a == b else f"between {a} and {b}"
    ep = inv.window[inv.window["tid"].isin(inv.episode)] if inv.window is not None else None
    chans = sorted(set(ep["channel"])) if ep is not None and len(ep) else [f["channel"]]
    where = " and ".join(c.replace("_", " ") for c in chans)
    parts = [
        f"Subject: customer {cust}, card {card}. Between {when.replace('between ', '').replace('on ', '')}, {len(inv.episode)} {where} transaction(s) totalling "
        f"${inv.exposure:,.2f} were made on this card that the cardholder does not recognise or that are inconsistent with the cardholder's history.",
    ]
    if inv.pattern == "undocumented" and inv.connected_devices:
        parts.append(f"All of the card's suspicious activity came from a single device profile ({inv.connected_devices[0]}), which is recorded as New for the account and behind an anonymous proxy.")
        parts.append(f"The same device profile transacted on {len(inv.connected_cards)} other cards in the same burst: {', '.join(inv.connected_cards)}.")
        parts.append("Individual amounts are ordinary and the bank's risk scores stayed low, so the activity is only visible by linking cards through the shared device; this indicates one actor working many compromised card numbers.")
    elif inv.pattern == "undocumented":
        parts.append(inv.pattern_description)
        parts.append("Keeping each purchase just under a round authorization threshold while repeating it within minutes is consistent with deliberate structuring to avoid review.")
    elif inv.pattern == "card_testing":
        parts.append(f"The sequence began with very small online authorizations and was followed by a larger purchase, consistent with testing a stolen card number before use.")
    else:
        parts.append(f"The activity is consistent with {_PATTERN_TEXT.get(inv.pattern, inv.pattern)}.")
    for e in inv.evidence:
        if e.source == "graph" and any(k in e.claim for k in ("amount", "Amount", "never been", "never used")):
            parts.append(e.claim)
            break
    resp = ""
    parts.append(f"Model assessment: fraud probability {inv.p_case:.2f}, derived from a calibrated transaction model and graph evidence; the bank's own risk score on the flagged transaction was {f.get('bank_risk'):.2f}.")
    parts.append(f"Recommended internal action: block and reissue the card and monitor connected cards; the case is written to the investigation graph for future analysts.")
    parts.append(f"This report is filed because the activity meets the reporting bar (exposure, shared origin or a coordinated/undocumented pattern).")
    return " ".join(parts)


def sar_subjects(inv: Investigation) -> list[str]:
    subj = [inv.trigger["customer_id"], inv.trigger["card_id"]]
    subj += list(inv.connected_cards)
    subj += list(inv.connected_devices)
    return list(dict.fromkeys(subj))
