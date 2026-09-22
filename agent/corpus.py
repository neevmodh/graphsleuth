"""GraphRAG corpus: the documents and typologies the agent retrieves from.

Four sources, each chunk keeping its source and title so an answer can cite it:
  policy       the bank's fraud policy (rules R1-R10, routing, case-vs-report, stop rule) and the five documented patterns
  typology     the closed cases' analyst narratives, de-duplicated: 5,565 cases collapse to ~390 distinct typologies, each
               linked to its most recent real example cases (EXEMPLIFIED_BY edges) so retrieval can hop into the graph
  regulatory   FinCEN SAR narrative guidance and the ATO advisory (US government publications, public domain)
Raw regulatory files are downloaded to data/store/corpus/ (git-ignored); MANIFEST lists their URLs.
"""
from __future__ import annotations

import html
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "data" / "store" / "corpus"
README = Path(os.getenv("DATA_DIR", str(ROOT.parent / "dataset" / "HHGOA_IEEE"))) / "README.md"
DB = ROOT / "data" / "store" / "graphsleuth.duckdb"

MANIFEST = {
    "fincen_sar_narrative_guidance": ("FinCEN: Guidance on Preparing a Complete and Sufficient SAR Narrative",
                                      "https://www.fincen.gov/system/files/shared/sar_guidance_narrative.pdf"),
    "fincen_sar_complete_narrative": ("FinCEN: Preparing a Complete and Sufficient Suspicious Activity Report Narrative",
                                      "https://www.fincen.gov/system/files/shared/sarnarrcompletguidfinal_112003.pdf"),
    "fincen_ato_advisory": ("FinCEN Advisory FIN-2011-A016: Account Takeover Activity",
                            "https://www.fincen.gov/resources/advisories/fincen-advisory-fin-2011-a016"),
}


@dataclass
class Chunk:
    id: str
    doc_source: str            # policy | typology | regulatory
    title: str
    body: str
    pattern: str = ""
    n_cases: int = 0
    examples: list[str] = field(default_factory=list)      # closed-case ids (typology chunks)

    @property
    def embed_text(self) -> str:
        return f"{self.title}. {self.body}"


def _words(text: str, n: int, overlap: int) -> list[str]:
    w = text.split()
    step = max(n - overlap, 1)
    return [" ".join(w[i:i + n]) for i in range(0, max(len(w) - overlap, 1), step) if w[i:i + n]]


# ---- policy + patterns -----------------------------------------------------------------------------------------
def policy_chunks() -> list[Chunk]:
    text = README.read_text()
    out: list[Chunk] = []
    pol = text[text.index("# Fraud Policy"):text.index("# Answer Format")]
    for m in re.finditer(r"\*\*(R\d+)\. ([^*]+)\*\*\s*(.+?)(?=\n\*\*R\d+\.|\n### |\Z)", pol, re.S):
        out.append(Chunk(f"POL-{m.group(1)}", "policy", f"Fraud policy {m.group(1)}: {m.group(2).strip()}", " ".join(m.group(3).split())))
    for m in re.finditer(r"### ([^\n]+)\n(.+?)(?=\n### |\Z)", pol, re.S):
        title, body = m.group(1).strip(), " ".join(m.group(2).split())
        if title.startswith("3. Rules"):
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
        for i, part in enumerate(_words(body, 170, 25)):
            out.append(Chunk(f"POL-S-{slug}-{i}", "policy", f"Fraud policy: {title}", part))
    pats = text[text.index("## The five known fraud patterns"):text.index("## Regulatory references")]
    for m in re.finditer(r"\*\*(\d)\. ([^*]+)\.\*\*\s*(.+?)(?=\n\*\*\d\.|\Z)", pats, re.S):
        out.append(Chunk(f"PAT-{m.group(1)}", "policy", f"Documented fraud pattern {m.group(1)}: {m.group(2).strip()}", " ".join(m.group(3).split())))
    tk = text[text.index("## Things to know"):text.index("## Rules")]
    out.append(Chunk("POL-KNOW", "policy", "Investigator guidance: things to know", " ".join(tk.split("\n", 1)[1].split())))
    return out


# ---- typologies from closed cases ------------------------------------------------------------------------------------
def _norm(t: str) -> str:
    t = re.sub(r"^Case CC-\d+: ", "", t)
    t = re.sub(r"cardholder C\d{5}(-K\d)?\b", "the cardholder", t)
    t = re.sub(r"\bC\d{5}(-K\d)?\b", "the card", t).replace("card the card", "the card")
    t = re.sub(r"\$[\d,]+\.?\d*", "an amount", t)
    t = re.sub(r"\d{4}-\d{2}-\d{2}", "a date", t)
    t = re.sub(r"\b\d+(\.\d+)?\b", "N", t)
    return t.strip()


def typology_chunks(cutoff: str = "2016-11-01") -> list[Chunk]:
    con = duckdb.connect(str(DB), read_only=True)
    rows = con.execute("""SELECT case_id, pattern, outcome, analyst_notes, report_filed, opened_at FROM closed_cases
                          WHERE opened_at < ? ORDER BY opened_at DESC""", [cutoff]).fetchall()
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(_norm(r[3]), []).append(r)
    out = []
    for i, (norm, rs) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1]))):
        pats = [r[1] for r in rs]
        pat = max(set(pats), key=pats.count)
        rep = sum(bool(r[4]) for r in rs) / len(rs)
        out.append(Chunk(f"TYPO-{i:04d}", "typology", f"Closed-case typology ({rs[0][2].replace('_', ' ')}, pattern {pat})",
                         f"{norm} Seen in {len(rs)} closed cases; {rep:.0%} led to a suspicious activity report.",
                         pattern=pat, n_cases=len(rs), examples=[r[0] for r in rs[:5]]))
    return out


# ---- regulatory documents -----------------------------------------------------------------------------------------------
def _fetch(key: str, url: str) -> Path:
    import urllib.request
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    f = CORPUS_DIR / (key + (".pdf" if url.endswith(".pdf") else ".html"))
    if not f.exists():
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        f.write_bytes(urllib.request.urlopen(req, timeout=60).read())
    return f


def _text_of(f: Path) -> str:
    if f.suffix == ".pdf":
        return subprocess.run(["pdftotext", "-layout", str(f), "-"], capture_output=True, text=True, check=True).stdout
    raw = f.read_text(errors="ignore")
    raw = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", raw)
    return html.unescape(re.sub(r"<[^>]+>", " ", raw))


def regulatory_chunks() -> list[Chunk]:
    out = []
    for key, (title, url) in MANIFEST.items():
        text = re.sub(r"\s+", " ", _text_of(_fetch(key, url))).strip()
        for i, part in enumerate(_words(text, 200, 30)):
            if len(part.split()) >= 60 and part.count("....") < 3 and "Table of Contents" not in part:   # skip tables of contents
                out.append(Chunk(f"REG-{key}-{i:03d}", "regulatory", title, part))
    return out


def build_corpus() -> list[Chunk]:
    return policy_chunks() + typology_chunks() + regulatory_chunks()


if __name__ == "__main__":
    import collections
    ch = build_corpus()
    print(len(ch), dict(collections.Counter(c.doc_source for c in ch)))
    for src in ("policy", "typology", "regulatory"):
        c = next(c for c in ch if c.doc_source == src)
        print(f"\n[{src}] {c.id} | {c.title}\n  {c.body[:260]}")
