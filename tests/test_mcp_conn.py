"""MCPConnection's pyTigerGraph-shaped facade (agent/mcp_conn.py), against a fake `.call()` -- no live MCP server or
network. These lock in the response shape the real tigergraph-mcp server actually returns (verified live,
2026-09-22: run_installed_query's envelope key is "result", singular -- a "results" typo here silently broke every
graph read/write once GRAPHSLEUTH_TG_VIA=mcp routed the whole backend through MCP, not just GraphRAG)."""
from agent.mcp_conn import MCPConnection


def _bare() -> MCPConnection:
    """An MCPConnection with no live session: __init__ starts a stdio subprocess and blocks on a handshake, so
    these tests bypass it and only exercise the pure response-parsing methods below."""
    return object.__new__(MCPConnection)


def test_run_installed_query_reads_the_singular_result_key(monkeypatch):
    conn = _bare()
    calls = []

    def fake_call(tool, **kw):
        calls.append((tool, kw))
        return {"query_name": kw["query_name"], "parameters": kw["params"],
                "result": [{"C": [{"v_id": "C00011-K1", "v_type": "Card", "attributes": {"card_id": "C00011-K1"}}]}]}

    monkeypatch.setattr(conn, "call", fake_call)
    out = conn.runInstalledQuery("customer_cards", params={"cust": ("C00011", "Customer")})
    assert out == [{"C": [{"v_id": "C00011-K1", "v_type": "Card", "attributes": {"card_id": "C00011-K1"}}]}]
    assert calls[0] == ("run_installed_query", {"query_name": "customer_cards", "params": {"cust": {"id": "C00011", "type": "Customer"}}})


def test_run_installed_query_falls_back_when_there_is_no_result_key(monkeypatch):
    conn = _bare()
    monkeypatch.setattr(conn, "call", lambda tool, **kw: {"rows": [1, 2, 3]})
    assert conn.runInstalledQuery("x") == {"rows": [1, 2, 3]}


def test_get_vertices_by_id_unwraps_the_node_envelope(monkeypatch):
    conn = _bare()
    monkeypatch.setattr(conn, "call", lambda tool, **kw: {"node": {"attributes": {"card4": "visa"}}})
    assert conn.getVerticesById("Card", "C1-K1") == [{"attributes": {"card4": "visa"}}]


def test_get_vertices_by_id_returns_empty_when_the_node_is_missing(monkeypatch):
    conn = _bare()
    monkeypatch.setattr(conn, "call", lambda tool, **kw: {"node": None})
    assert conn.getVerticesById("Card", "C1-K1") == []


def test_upsert_vertex_and_edge_pass_the_right_tool_and_args(monkeypatch):
    conn = _bare()
    seen = []
    monkeypatch.setattr(conn, "call", lambda tool, **kw: seen.append((tool, kw)) or {})
    conn.upsertVertex("FraudCase", "CASE-2016-014", {"verdict": "fraud"})
    conn.upsertEdge("Card", "C1-K1", "HAS_CASE", "FraudCase", "CASE-2016-014")
    assert seen[0] == ("add_node", {"vertex_type": "FraudCase", "vertex_id": "CASE-2016-014", "attributes": {"verdict": "fraud"}})
    assert seen[1] == ("add_edge", {"source_vertex_type": "Card", "source_vertex_id": "C1-K1", "edge_type": "HAS_CASE",
                                    "target_vertex_type": "FraudCase", "target_vertex_id": "CASE-2016-014", "attributes": {}})


def test_get_edges_filters_by_type(monkeypatch):
    conn = _bare()
    monkeypatch.setattr(conn, "call", lambda tool, **kw: {"edges": [{"e_type": "HAS_CASE", "to_id": "A"}, {"e_type": "OTHER", "to_id": "B"}]})
    assert conn.getEdges("Card", "C1-K1", "HAS_CASE") == [{"e_type": "HAS_CASE", "to_id": "A"}]
    assert len(conn.getEdges("Card", "C1-K1")) == 2
