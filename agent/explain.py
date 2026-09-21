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
    "summary": ("You edit fraud-case summaries for analysts. Rewrite the text to be clear and concise (2 to 6 sentences). "
                "Do not add, remove or change any fact, number, amount, date or identifier, and do not speculate. "
                "Keep every digit exactly as written. Output only the rewritten text."),
    "sar": ("You edit Suspicious Activity Report narratives for a regulator. Rewrite the text in a clear, professional, neutral tone. "
            "Keep the who, what, when, where, how and why structure and between 6 and 12 sentences. Do not add, remove or change any fact, "
            "number, amount, date, card or customer identifier, and do not speculate or accuse. Keep every digit exactly as written. "
            "Output only the rewritten narrative."),
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


def polish(router: LLMRouter | None, text: str, kind: str) -> Polished:
    """Return the LLM-polished text if it passes every guard, otherwise the original."""
    if router is None or not router.available:
        return Polished(text, False, "no LLM configured")
    try:
        r = router.chat([{"role": "system", "content": SYSTEM[kind]}, {"role": "user", "content": text}], role="synth",
                        temperature=0.0, max_tokens=1200)
    except LLMUnavailable as e:
        return Polished(text, False, f"LLM unavailable: {str(e)[:80]}")
    out = r.text.strip().strip('"')
    if not out:
        return Polished(text, False, "empty response")
    ok, why = facts_preserved(text, out)
    if not ok:
        return Polished(text, False, f"fact guard rejected ({why})")
    if not 0.6 <= len(out) / max(len(text), 1) <= 1.6:
        return Polished(text, False, "length out of range")
    if kind == "sar" and not 6 <= _sentences(out) <= 14:
        return Polished(text, False, f"sentence count {_sentences(out)} out of range")
    return Polished(out, True, f"polished by {r.provider}")
