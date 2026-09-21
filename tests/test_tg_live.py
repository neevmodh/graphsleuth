"""Live check of the TigerGraph backend. Skipped unless TG_HOST is set (Savanna workspace with the graph loaded and the
queries installed: gsql -g GraphSleuth graph/queries.gsql ; INSTALL QUERY ALL).

Passes when the TigerGraph backend reproduces the local backend's answer for the ring case and a false-alarm case."""
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")   # so skipif sees TG_HOST regardless of import order

pytestmark = pytest.mark.skipif(not os.getenv("TG_HOST"), reason="TG_HOST not set: no live TigerGraph instance")


def _answer(backend, memory, case_id):
    import pandas as pd
    from agent.orchestrator import Orchestrator
    data_dir = os.getenv("DATA_DIR", "../dataset/HHGOA_IEEE")
    trig = pd.read_csv(f"{data_dir}/case_pack.csv").set_index("case_id").loc[case_id].to_dict()
    trig["case_id"] = case_id
    return Orchestrator(backend, memory).run_case(trig, write_memory=False)


@pytest.mark.parametrize("case_id", ["HHG-014", "HHG-010"])
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
