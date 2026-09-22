"""Live check of the TigerGraph backend (#11: unit-test each query against known closed cases). Skipped unless TG_HOST
is set (Savanna workspace with the graph loaded and the queries installed: gsql -g GraphSleuth graph/queries.gsql ;
INSTALL QUERY ALL).

`test_tigergraph_reproduces_local_answer` runs six benchmark cases spanning five documented patterns plus a legitimate
alert through BOTH backends and checks they agree; between them these cases exercise every query in the original
toolkit (card_window/get_transaction/card_profile always; device_neighbors+device_burst on HHG-014's ring;
region_history+region_cluster on HHG-001's out-of-region trip; recurring_txns and closed_cases_card/device/region on
whichever apply). The local backend is itself the ground truth here: `eval/eval_results.md` is where it was checked
against the closed cases' own labels. The remaining tests check the graph-algorithm queries (#9) against the one
ring this dataset actually has (`docs/data_findings.md`): 52 cards, one device, always New, always proxied."""
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")   # so skipif sees TG_HOST regardless of import order

pytestmark = pytest.mark.skipif(not os.getenv("TG_HOST"), reason="TG_HOST not set: no live TigerGraph instance")

RING_DEVICE = "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080"
RING_SIZE = 52
RING_CARD = "C10193-K2"          # any card known to be on the ring device


def _answer(backend, memory, case_id, write_memory=False):
    import pandas as pd
    from agent.orchestrator import Orchestrator
    data_dir = os.getenv("DATA_DIR", "../dataset/HHGOA_IEEE")
    trig = pd.read_csv(f"{data_dir}/case_pack.csv").set_index("case_id").loc[case_id].to_dict()
    trig["case_id"] = case_id
    return Orchestrator(backend, memory).run_case(trig, write_memory=write_memory)


@pytest.mark.parametrize("case_id", ["HHG-014", "HHG-010", "HHG-001", "HHG-002", "HHG-007", "HHG-005"])
def test_tigergraph_reproduces_local_answer(case_id):
    from agent.backend import LocalBackend
    from agent.tg_backend import TigerGraphBackend
    local = _answer(LocalBackend("final"), None, case_id)
    live = _answer(TigerGraphBackend(), None, case_id)
    assert live.case.verdict == local.case.verdict
    assert live.case.pattern == local.case.pattern
    assert set(live.case.affected_txn_ids) == set(local.case.affected_txn_ids)
    assert abs(live.case.exposure_usd - local.case.exposure_usd) < 0.01
    assert set(live.case.connected_card_ids) == set(local.case.connected_card_ids)


def test_find_rings_returns_exactly_the_one_known_ring():
    from agent.tg_backend import TigerGraphBackend
    rings = TigerGraphBackend().find_rings(min_cards=8, min_new=0.9, min_proxy=0.9)
    assert len(rings) == 1
    assert rings[0]["device_profile"] == RING_DEVICE and rings[0]["n_cards"] == RING_SIZE


def test_ring_component_matches_the_known_ring_exactly():
    from agent.tg_backend import TigerGraphBackend
    comp = TigerGraphBackend().ring_component(RING_CARD, max_device_cards=80, min_new_frac=0.85, min_proxy_frac=0.85, max_hops=6)
    assert comp["n_cards"] == RING_SIZE and comp["n_devices"] == 1
    assert comp["devices"][0]["device_profile"] == RING_DEVICE
    assert {c["card_id"] for c in comp["cards"]} >= {RING_CARD}


def test_device_hub_rank_puts_the_known_ring_device_first_by_a_wide_margin():
    from agent.tg_backend import TigerGraphBackend
    out = TigerGraphBackend().device_hub_rank(max_device_cards=80, min_new_frac=0.85, min_proxy_frac=0.85, iters=4, top_k=5)
    assert out[0]["device_profile"] == RING_DEVICE
    assert out[0]["hub_score"] > 10 * out[1]["hub_score"]         # dominant separation from the next-best device


def test_card_link_reports_two_ring_cards_as_linked():
    from agent.tg_backend import TigerGraphBackend
    b = TigerGraphBackend()
    other = next(c["card_id"] for c in b.ring_component(RING_CARD)["cards"] if c["card_id"] != RING_CARD)
    linked = b.card_link(RING_CARD, other)
    assert linked["linked"] and linked["component_size"] == RING_SIZE


VENV_MCP = Path(__file__).resolve().parents[1] / ".venv-mcp" / "bin" / "tigergraph-mcp"


@pytest.mark.skipif(not VENV_MCP.exists(), reason=".venv-mcp not set up: pip install -r requirements-mcp.txt into it")
def test_mcp_backend_reproduces_local_answer():
    """GRAPHSLEUTH_TG_VIA=mcp: the whole backend routed through the MCP server (not just GraphRAG's retrieval),
    using MCPConnection's pyTigerGraph-shaped facade end to end -- read (runInstalledQuery, getVerticesById) and
    write (upsertVertex, upsertEdge) both exercised, since write_memory stays on here."""
    from agent.backend import LocalBackend
    from agent.mcp_conn import MCPConnection
    from agent.tg_backend import TigerGraphBackend, TigerGraphCaseMemory
    conn = MCPConnection()
    try:
        local = _answer(LocalBackend("final"), None, "HHG-014")
        mcp_answer = _answer(TigerGraphBackend(conn), TigerGraphCaseMemory(conn), "HHG-014", write_memory=True)
    finally:
        conn.close()
    assert mcp_answer.case.verdict == local.case.verdict
    assert mcp_answer.case.pattern == local.case.pattern
    assert abs(mcp_answer.case.exposure_usd - local.case.exposure_usd) < 0.01
    assert mcp_answer.case.written_to_graph and mcp_answer.case.graph_case_id == "CASE-2016-014"
