"""TigerGraphBackend against a FAKE connection. These tests check parsing, the -1 sentinel, parameter shapes and that the
output feeds the context model. They do NOT prove the wire protocol: that needs a live instance (tests/test_tg_live.py)."""
import math

import pandas as pd

from agent.episode import feats_for_window
from agent.schemas import Case
from agent.tg_backend import TigerGraphBackend, TigerGraphCaseMemory


def txn(tid, ts, amt=10.0, ch="online", **kw):
    a = dict(tid=tid, ts=ts, amt=amt, prod="C", channel=ch, addr1=-1.0, bank_risk=0.3, p=0.4, p_oof=0.2, dev_new=-1, dev_unseen=-1,
             addr1_new=-1, prod_new=0, via_proxy=0, amt_ratio_med=-1.0, gap_prev_h=-1.0, n_1h=1, n_small_1h=0, addr1_prior_n=0,
             prior_n=5, pat_ato=0.1, pat_cnp=0.6, pat_cnpnd=0.1, pat_oor=0.1, pat_none=0.1, card_id="C1-K1")
    a.update(kw)
    return {"v_id": str(tid), "v_type": "Transaction", "attributes": a}


class FakeConn:
    def __init__(self, responses):
        self.responses, self.calls, self.upserts, self.edges = responses, [], [], []

    def runInstalledQuery(self, name, params=None, timeout=None, sizeLimit=None):
        self.calls.append((name, params))
        return self.responses[name]

    def getVerticesById(self, vtype, vid):
        return [{"attributes": {"card4": "visa", "card6": ""}}]

    def upsertVertex(self, *a):
        self.upserts.append(a)

    def upsertEdge(self, *a):
        self.edges.append(a)


def test_card_window_parses_sentinel_and_feeds_the_context_model():
    conn = FakeConn({"card_window": [
        {"T": [txn(1, "2016-12-01 10:00:00"), txn(2, "2016-12-01 11:00:00", dev_new=1, addr1=204.0)]},
        {"device_by_tid": {"2": "PHONE | Android 7.0 | chrome | 1920x1080"}}]})
    b = TigerGraphBackend(conn)
    w = b.card_window("C1-K1", "2016-12-01 10:30:00", 72, 72)
    assert conn.calls[0][1]["card"] == ("C1-K1", "Card")                  # vertex params are (id, type) tuples
    assert list(w["tid"]) == [1, 2]
    assert math.isnan(w.loc[0, "dev_new"]) and w.loc[1, "dev_new"] == 1   # -1 sentinel -> NaN
    assert w.loc[1, "device_profile"].startswith("PHONE") and pd.isna(w.loc[0, "device_profile"])
    feats = feats_for_window(w, 2)                                        # the same code path the agent uses
    assert len(feats) == 2 and feats.loc[feats.tid == 2, "same_device"].iloc[0] == 1.0


def test_transaction_returns_card_network_and_maps_pattern_columns():
    conn = FakeConn({"get_transaction": [{"Start": [txn(7, "2016-12-01 10:00:00")]}, {"device_by_tid": {}}]})
    t = TigerGraphBackend(conn).transaction(7)
    assert t["card4"] == "visa" and t["card6"] is None                    # empty string -> None
    assert t["pat_card_not_present_fraud"] == 0.6 and t["proxy"] == 0


def test_device_burst_groups_by_gap_and_names_the_cards():
    rows = [{"ts": "2016-11-14 10:00:00", "card": "A", "tid": 1, "amt": 5.0, "p": 0.1},
            {"ts": "2016-11-15 10:00:00", "card": "B", "tid": 2, "amt": 6.0, "p": 0.1},
            {"ts": "2016-08-15 10:00:00", "card": "C", "tid": 3, "amt": 7.0, "p": 0.1}]
    b = TigerGraphBackend(FakeConn({"device_txns": [{"rows": rows}]}))
    burst = b.device_burst("DEV", "2016-11-14 12:00:00")
    assert sorted(c["card_id"] for c in burst["cards"]) == ["A", "B"]     # the August activity is a different burst


def test_recurring_check_needs_true_monthly_gaps():
    tx = {"get_transaction": [{"Start": [txn(9, "2016-12-10 13:00:00", amt=49.0)]}, {"device_by_tid": {}}]}
    monthly = ["2016-10-11 13:00:00", "2016-11-10 13:00:00", "2016-12-10 13:00:00"]
    ragged = ["2016-08-13 13:00:00", "2016-09-02 13:00:00", "2016-09-27 13:00:00", "2016-12-10 13:00:00"]   # 20, 25, 74 day gaps
    assert TigerGraphBackend(FakeConn({**tx, "recurring_txns": [{"ts": monthly}]})).recurring_check("C1-K1", 9)["is_recurring"]
    assert not TigerGraphBackend(FakeConn({**tx, "recurring_txns": [{"ts": ragged}]})).recurring_check("C1-K1", 9)["is_recurring"]


def test_similar_cases_scores_and_excludes_the_case_itself():
    def cc(i, pat):
        return {"v_id": i, "attributes": {"case_id": i, "outcome": "confirmed_fraud", "pattern": pat, "exposure": 10.0, "opened_at": "2016-09-01 00:00:00"}}
    conn = FakeConn({"closed_cases_card": [{"A": [cc("CC-1", "x")]}], "closed_cases_device": [{"B": [cc("CC-2", "undocumented"), cc("CC-9", "undocumented")]}],
                     "closed_cases_region": [{"C": [cc("CC-3", "x")]}]})
    out = TigerGraphBackend(conn).similar_cases(card_id="C1-K1", device_profile="DEV", addr1=204.0, pattern="undocumented",
                                                before_ts="2016-12-01 00:00:00", exclude_case="CC-9")
    assert [c["case_id"] for c in out] == ["CC-2", "CC-1", "CC-3"]        # device+pattern (5) > card (3) > region (1)
    assert out[0]["score"] == 5 and all(c["case_id"] != "CC-9" for c in out)


def test_case_memory_writes_the_case_and_its_links():
    conn = FakeConn({})
    case = Case(status="closed_fraud", verdict="fraud", fraud_probability=0.9, pattern="undocumented", pattern_description="ring",
                affected_txn_ids=["1", "2"], connected_card_ids=["C2-K1"], connected_device_profiles=["DEV"], exposure_usd=20.0,
                similar_prior_cases=["CC-1"], summary="s")
    gid, written = TigerGraphCaseMemory(conn).write({"case_id": "HHG-014", "card_id": "C1-K1"}, case, None, [])
    assert gid == "CASE-2016-014" and written is True
    kinds = [e[2] for e in conn.edges]
    assert kinds.count("CASE_TXN") == 2 and {"HAS_CASE", "CASE_DEVICE", "CASE_CONNECTED", "SIMILAR_TO"} <= set(kinds)
    assert conn.upserts[0][0] == "FraudCase"
