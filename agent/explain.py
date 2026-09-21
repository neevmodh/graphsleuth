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
