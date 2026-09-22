"""LLM polish with a fact guard.

The template text (agent/narrative.py) is built from structured evidence and is the source of truth. An LLM may reword it
for clarity, but it may not add, drop or change a fact. Every number, amount, date and identifier in the original must
appear in the result and the result may contain no others; otherwise the original is kept. A regulator-facing report must
never contain a figure the evidence does not support.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .llm import LLMRouter, LLMUnavailable

_NUM = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")
_ID = re.compile(r"\b(?:C\d{5}(?:-K\d+)?|CC-\d{4}|HHG-\d{3}|CASE-\d{4}-\d+|R\d{1,2})\b")

SYSTEM = {
    "summary": ("You edit fraud-case summaries for analysts. Improve the wording and flow of the text WITHOUT shortening it or dropping "
                "anything. Every number, amount, date, time, percentage, card id, customer id, case id and rule id in the input must "
                "appear in your output exactly as written (keep times like 20:00 and amounts like $1,906.07 verbatim). Do not add any "
                "new number, fact or speculation. You may reorder and join sentences. Output only the rewritten text."),
    "sar": ("You edit Suspicious Activity Report narratives for a regulator. Improve clarity and professional tone WITHOUT shortening "
            "or dropping anything. Keep the Who / What / When / Where / How / Why structure and 6 to 12 sentences. Every number, amount, "
            "date, time, percentage, probability, card id, customer id, case id and rule id in the input must appear in your output "
            "exactly as written (keep times like 20:00 and amounts like $1,906.07 verbatim). Do not add any new number, fact, "
            "accusation or speculation. Output only the rewritten narrative."),
}


def critical_tokens(text: str) -> set[str]:
    """Numbers (normalised: no $ or thousands commas) and identifiers that must survive a rewrite."""
    nums = {m.group(0).replace("$", "").replace(",", "").rstrip(".") for m in _NUM.finditer(text)}
    return nums | set(_ID.findall(text))


def facts_preserved(original: str, rewritten: str) -> tuple[bool, str]:
    a, b = critical_tokens(original), critical_tokens(rewritten)
    if a - b:
        return False, f"dropped: {sorted(a - b)[:5]}"
    if b - a:
        return False, f"invented: {sorted(b - a)[:5]}"
    return True, "ok"


def _sentences(t: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", t.strip()) if s])


@dataclass
class Polished:
    text: str
    used_llm: bool
    reason: str


def polish(router: LLMRouter | None, text: str, kind: str, attempts: int = 2) -> Polished:
    """Return the LLM-polished text if it passes every guard, otherwise the original.

    If a rewrite drops or invents a fact, the model gets one repair round that names exactly which tokens went wrong."""
    if router is None or not router.available:
        return Polished(text, False, "no LLM configured")
    messages = [{"role": "system", "content": SYSTEM[kind]}, {"role": "user", "content": text}]
    reason = "no attempt"
    for attempt in range(attempts):
        try:
            r = router.chat(messages, role="synth", temperature=0.0, max_tokens=1600)
        except LLMUnavailable as e:
            return Polished(text, False, f"LLM unavailable: {str(e)[:80]}")
        out = r.text.strip().strip('"')
        if not out:
            reason = "empty response"
            continue
        ok, why = facts_preserved(text, out)
        if not ok:
            reason = f"fact guard rejected ({why})"
            a, b = critical_tokens(text), critical_tokens(out)
            fix = []
            if a - b:
                fix.append("You dropped these values; include every one exactly as written: " + ", ".join(sorted(a - b)))
            if b - a:
                fix.append("You introduced these values that are not in the input; remove them: " + ", ".join(sorted(b - a)))
            messages = messages + [{"role": "assistant", "content": out}, {"role": "user", "content": " ".join(fix) + " Rewrite the full text again."}]
            continue
        if not 0.6 <= len(out) / max(len(text), 1) <= 1.6:
            reason = "length out of range"
            continue
        if kind == "sar" and not 6 <= _sentences(out) <= 14:
            reason = f"sentence count {_sentences(out)} out of range"
            continue
        return Polished(out, True, f"polished by {r.provider}" + (" after repair" if attempt else ""))
    return Polished(text, False, reason)


RATIONALE_SYSTEM = ("You are a fraud analyst. Using ONLY the facts and the reference context below, write two sentences explaining why the "
                    "recommended actions follow from the bank's policy. Cite rule ids (like R2) or chunk ids (like POL-R2) exactly as they "
                    "appear in the context. Do not introduce any number, amount, date or identifier that is not in the facts or the context. "
                    "Output only the two sentences.")


def rationale(router: LLMRouter | None, facts: str, context: str) -> Polished:
    """Two grounded sentences on why the actions follow from policy. Rejected unless every number and ID in it appears in the
    facts or the retrieved context (the same fact guard as the polish step)."""
    if router is None or not router.available or not context.strip():
        return Polished("", False, "no LLM or no context")
    try:
        r = router.chat([{"role": "system", "content": RATIONALE_SYSTEM},
                         {"role": "user", "content": f"FACTS:\n{facts}\n\nREFERENCE CONTEXT:\n{context}"}],
                        role="synth", temperature=0.0, max_tokens=500)
    except LLMUnavailable as e:
        return Polished("", False, f"LLM unavailable: {str(e)[:60]}")
    out = r.text.strip().strip('"')
    invented = critical_tokens(out) - critical_tokens(facts + " " + context)
    if not out or invented:
        return Polished("", False, f"rejected ({'empty' if not out else 'invented ' + str(sorted(invented)[:4])})")
    return Polished(out, True, f"grounded by {r.provider}")


SAR_GROUND_SYSTEM = ("You are a compliance analyst. Using ONLY the SAR narrative and the regulatory guidance below, write one sentence "
                     "naming which FinCEN/regulatory guidance this report follows, citing the chunk id exactly as it appears in the "
                     "guidance (like REG-1). Do not introduce any number, amount, date or identifier that is not already in the "
                     "narrative or the guidance. Output only the one sentence.")


def sar_grounding(router: LLMRouter | None, sar_text: str, context: str) -> Polished:
    """One sentence citing the regulatory guidance chunk(s) the SAR follows (from GraphRAG's `regulatory` source), or
    nothing if there is no LLM, no retrieved guidance, or the sentence would invent a fact."""
    if router is None or not router.available or not context.strip():
        return Polished("", False, "no LLM or no regulatory context")
    try:
        r = router.chat([{"role": "system", "content": SAR_GROUND_SYSTEM},
                         {"role": "user", "content": f"NARRATIVE:\n{sar_text}\n\nREGULATORY GUIDANCE:\n{context}"}],
                        role="synth", temperature=0.0, max_tokens=200)
    except LLMUnavailable as e:
        return Polished("", False, f"LLM unavailable: {str(e)[:60]}")
    out = r.text.strip().strip('"')
    invented = critical_tokens(out) - critical_tokens(sar_text + " " + context)
    if not out or invented:
        return Polished("", False, f"rejected ({'empty' if not out else 'invented ' + str(sorted(invented)[:4])})")
    return Polished(out, True, f"grounded by {r.provider}")
