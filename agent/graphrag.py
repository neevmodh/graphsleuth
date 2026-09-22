"""GraphRAG: retrieve grounded context from TigerGraph, then hand the LLM a compact pack instead of raw rows.

Index (offline, `python -m agent.graphrag build`):
  chunks (agent/corpus.py) -> Gemini embeddings (768-d, paced for the free tier) -> DocChunk vertices in TigerGraph,
  EXEMPLIFIED_BY edges to real closed cases, and the embeddings stored in the TigerVector attribute DocChunk.emb.
Retrieve (per alert): embed a description of the alert -> top-k similarity search over DocChunk.emb (through the TigerGraph MCP
  server) -> for typology hits, hop along EXEMPLIFIED_BY to actual closed cases -> a `RagContext` of policy / typology /
  regulatory hits with citations. Reads and writes to TigerGraph go over MCP.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field

from .corpus import build_corpus
from .llm import LLMRouter, LLMUnavailable


@dataclass
class Hit:
    chunk_id: str
    score: float
    source: str
    title: str
    body: str
    n_cases: int = 0
    examples: list[dict] = field(default_factory=list)       # real closed cases reached through EXEMPLIFIED_BY


@dataclass
class RagContext:
    query: str
    hits: list[Hit] = field(default_factory=list)

    def by_source(self, source: str) -> list[Hit]:
        return [h for h in self.hits if h.source == source]

    def pack(self, max_chars: int = 1800, sources: set[str] | None = None) -> str:
        """The compact context handed to the LLM: cited snippets, not raw graph rows. `sources` restricts to a subset
        (e.g. {"regulatory"} for FinCEN/AML guidance only, when grounding a SAR narrative rather than a rationale)."""
        parts = []
        for h in self.hits:
            if sources is not None and h.source not in sources:
                continue
            ex = f" Example closed cases: {', '.join(e['case_id'] for e in h.examples[:3])}." if h.examples else ""
            parts.append(f"[{h.chunk_id}] {h.title}: {h.body[:420]}{ex}")
        return "\n".join(parts)[:max_chars]


# ---- index ---------------------------------------------------------------------------------------------------------------
def embed_paced(router: LLMRouter, texts: list[str], batch: int = 30, per_min: int = 170) -> list[list[float]]:
    """Embed within the free tier's ~100 inputs/minute/key: pace batches, and wait out 429s instead of failing."""
    out: list[list[float]] = []
    min_gap = 60.0 * batch / per_min
    for i in range(0, len(texts), batch):
        t0 = time.time()
        for attempt in range(8):
            try:
                out += router.embed(texts[i:i + batch], batch=batch)
                break
            except LLMUnavailable:
                time.sleep(35)                                   # quota window; the same batch is retried (and cached once done)
        else:
            raise RuntimeError(f"embedding batch {i} failed after retries: {router.errors[-2:]}")
        print(f"  embedded {min(i + batch, len(texts))}/{len(texts)}", flush=True)
        time.sleep(max(0.0, min_gap - (time.time() - t0)))
    return out


def build_index(limit: int | None = None) -> dict:
    import pandas as pd
    from data.load_tg import connect, edges, vertices
    from .mcp_conn import MCPConnection

    chunks = build_corpus()[:limit]
    router = LLMRouter.from_env()
    print(f"corpus: {len(chunks)} chunks; embedding with {list(router.providers)}", flush=True)
    vecs = embed_paced(router, [c.embed_text for c in chunks])
    conn = connect()
    df = pd.DataFrame([{"id": c.id, "doc_source": c.doc_source, "title": c.title, "body": c.body, "pattern": c.pattern, "n_cases": c.n_cases}
                       for c in chunks])
    vertices(conn, df, "DocChunk", "id", ["doc_source", "title", "body", "pattern", "n_cases"], size=200, desc="DocChunk")
    ex = pd.DataFrame([{"chunk": c.id, "case": cid} for c in chunks for cid in c.examples])
    if len(ex):
        edges(conn, ex, "DocChunk", "EXEMPLIFIED_BY", "ClosedCase", "chunk", "case", desc="EXEMPLIFIED_BY")
    mcp = MCPConnection()
    try:
        for i in range(0, len(chunks), 25):
            batch = [{"vertex_id": c.id, "vector": v} for c, v in zip(chunks[i:i + 25], vecs[i:i + 25])]
            mcp.call("upsert_vectors", vertex_type="DocChunk", vector_attribute="emb", vectors=batch)
        n = mcp.call("get_vertex_count", vertex_type="DocChunk")
    finally:
        mcp.close()
    return {"chunks": len(chunks), "edges": len(ex), "tokens": router.tokens, "docchunk_count": n}


# ---- retrieve --------------------------------------------------------------------------------------------------------------
class GraphRAG:
    def __init__(self, router: LLMRouter, mcp, conn=None, k: int = 40):
        self.router, self.mcp, self.k, self.conn = router, mcp, k, conn

    @staticmethod
    def _parse(res: dict) -> list[tuple[str, float, dict]]:
        """(chunk id, cosine distance, attributes), closest first. The vertex list in the MCP result is NOT sorted by
        distance: the ranking comes from the separate `distances` map."""
        verts, dist = [], {}
        for part in res.get("result", []):
            verts += part.get("v", [])
            dist.update(part.get("distances", {}))
        rows = [(v["v_id"], float(dist.get(v["v_id"], 9.0)), v["attributes"]) for v in verts]
        return sorted(rows, key=lambda r: r[1])

    def retrieve(self, query: str, per_source: dict[str, int] | None = None) -> RagContext:
        """Top-k vector search in TigerGraph, then up to `per_source` hits from each source, plus graph hops for typologies."""
        want = per_source or {"policy": 2, "typology": 2, "regulatory": 1}
        vec = self.router.embed([query])[0]
        res = self.mcp.call("search_top_k_similarity", vertex_type="DocChunk", vector_attribute="emb", query_vector=vec, top_k=self.k)
        ctx, taken = RagContext(query), {s: 0 for s in want}
        for cid, d, a in self._parse(res):
            src = a.get("doc_source")
            if taken.get(src, 0) >= want.get(src, 0):
                continue
            hit = Hit(cid, round(1.0 - d, 4), src, a.get("title", ""), a.get("body", ""), int(a.get("n_cases") or 0))   # similarity = 1 - cosine distance
            if src == "typology" and self.conn is not None:                       # hop: typology -> real closed cases
                try:
                    out = self.conn.runInstalledQuery("chunk_examples", params={"chunk": (cid, "DocChunk")})
                    hit.examples = [v["attributes"] for r_ in out for v in (r_.get("C") or [])]
                except Exception:                                                  # noqa: BLE001
                    hit.examples = []
            ctx.hits.append(hit)
            taken[src] = taken.get(src, 0) + 1
        return ctx


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        print(build_index(int(sys.argv[2]) if len(sys.argv) > 2 else None))
