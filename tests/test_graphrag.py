"""GraphRAG retrieval and grounded rationale, with fake router / MCP / connection (no network)."""
from pathlib import Path

import pytest

from agent import explain
from agent.graphrag import GraphRAG, RagContext, Hit
from agent.llm import LLMResult


def vert(cid, src, title="t", body="b", n=0):
    return {"v_id": cid, "v_type": "DocChunk", "attributes": {"id": cid, "doc_source": src, "title": title, "body": body, "pattern": "", "n_cases": n}}


class FakeRouter:
    def embed(self, texts, batch=64):
        return [[0.0] * 4 for _ in texts]


class FakeMCP:
    """Mimics the real result shape: vertices are NOT in distance order; a separate `distances` map carries the ranking."""
    def __init__(self, verts, dist):
        self.verts, self.dist, self.calls = verts, dist, []

    def call(self, tool, **kw):
        self.calls.append((tool, kw))
        return {"result": [{"v": self.verts}, {"distances": self.dist}]}


class FakeConn:
    def runInstalledQuery(self, name, params=None, **kw):
        return [{"C": [{"attributes": {"case_id": "CC-0001"}}, {"attributes": {"case_id": "CC-0002"}}]}]


def test_ranking_uses_the_distances_map_not_the_list_order():
    verts = [vert("TYPO-1", "typology"), vert("POL-R7", "policy"), vert("POL-R2", "policy")]
    dist = {"POL-R2": 0.25, "POL-R7": 0.30, "TYPO-1": 0.33}
    ctx = GraphRAG(FakeRouter(), FakeMCP(verts, dist), conn=FakeConn()).retrieve("customer denies")
    assert [h.chunk_id for h in ctx.hits][:3] == ["POL-R2", "POL-R7", "TYPO-1"] or [h.chunk_id for h in ctx.by_source("policy")] == ["POL-R2", "POL-R7"]
    assert ctx.by_source("policy")[0].chunk_id == "POL-R2"                       # the closest, though listed last
    assert ctx.by_source("policy")[0].score == pytest.approx(0.75)                # similarity = 1 - cosine distance


def test_per_source_caps_and_typology_hops_to_real_cases():
    verts = [vert(f"POL-{i}", "policy") for i in range(5)] + [vert("TYPO-1", "typology", n=947), vert("REG-1", "regulatory")]
    dist = {v["v_id"]: 0.1 + 0.01 * i for i, v in enumerate(verts)}
    ctx = GraphRAG(FakeRouter(), FakeMCP(verts, dist), conn=FakeConn()).retrieve("q", per_source={"policy": 2, "typology": 1, "regulatory": 1})
    assert len(ctx.by_source("policy")) == 2 and len(ctx.by_source("typology")) == 1 and len(ctx.by_source("regulatory")) == 1
    assert [e["case_id"] for e in ctx.by_source("typology")[0].examples] == ["CC-0001", "CC-0002"]     # graph hop
    assert "CC-0001" in ctx.pack() and "TYPO-1" in ctx.pack()


def test_pack_is_bounded():
    ctx = RagContext("q", [Hit(f"POL-{i}", 0.9, "policy", "title", "x" * 900) for i in range(10)])
    assert len(ctx.pack(max_chars=1800)) <= 1800


def _router(text):
    class R:
        available = True
        errors = []
        def chat(self, messages, **kw):
            return LLMResult(text=text, provider="fake", model="m")
    return R()


FACTS = "verdict fraud, fraud probability 0.92, exposure $439.61; final actions: BLOCK_CARD (R2: customer denies the transaction)"
CONTEXT = "[POL-R2] Fraud policy R2: Customer denies the transaction. Recommend BLOCK_CARD and CREATE_CASE."


def test_rationale_accepts_a_grounded_answer():
    r = explain.rationale(_router("Rule R2 (POL-R2) applies because the customer denied the transaction, so the card is blocked."), FACTS, CONTEXT)
    assert r.used_llm and "POL-R2" in r.text


def test_rationale_rejects_an_invented_number():
    r = explain.rationale(_router("Rule R2 applies and 15 other cards are also affected."), FACTS, CONTEXT)
    assert not r.used_llm and "invented" in r.reason


def test_rationale_needs_context_and_an_llm():
    assert not explain.rationale(None, FACTS, CONTEXT).used_llm
    assert not explain.rationale(_router("x"), FACTS, "").used_llm


SAR_TEXT = ("Who: customer C10193, holder of visa card C10193-K2, is the subject of this report. "
            "What: 1 online transaction totalling $1,906.07 was made on the card on 2016-12-01.")
REG_CONTEXT = "[REG-1] FinCEN SAR narrative guidance: describe who, what, when, where, why and how in a standalone narrative."


def test_sar_grounding_cites_the_regulatory_chunk():
    r = explain.sar_grounding(_router("This report follows FinCEN SAR narrative guidance (REG-1)."), SAR_TEXT, REG_CONTEXT)
    assert r.used_llm and "REG-1" in r.text


def test_sar_grounding_rejects_an_invented_amount():
    r = explain.sar_grounding(_router("Filed per REG-1, covering an additional $500 not previously reported."), SAR_TEXT, REG_CONTEXT)
    assert not r.used_llm and "invented" in r.reason


def test_sar_grounding_needs_context_and_an_llm():
    assert not explain.sar_grounding(None, SAR_TEXT, REG_CONTEXT).used_llm
    assert not explain.sar_grounding(_router("x"), SAR_TEXT, "").used_llm


def test_pack_sources_filters_to_the_requested_source():
    ctx = RagContext("q", [Hit("POL-R2", 0.9, "policy", "t", "policy body"), Hit("REG-1", 0.8, "regulatory", "t2", "reg body")])
    only_reg = ctx.pack(sources={"regulatory"})
    assert "REG-1" in only_reg and "POL-R2" not in only_reg
    assert "POL-R2" in ctx.pack() and "REG-1" in ctx.pack()          # no filter: everything


README = Path(__file__).resolve().parents[2] / "dataset" / "HHGOA_IEEE" / "README.md"


@pytest.mark.skipif(not README.exists(), reason="dataset README not present")
def test_policy_chunks_cover_the_rules_and_patterns():
    from agent.corpus import policy_chunks
    ids = {c.id for c in policy_chunks()}
    assert {f"POL-R{i}" for i in range(1, 11)} <= ids and {f"PAT-{i}" for i in range(1, 6)} <= ids
