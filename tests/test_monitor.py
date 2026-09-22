"""run_monitor.py's alert-selection logic (#26), against a fake backend / an in-memory DuckDB. Does not exercise the
TigerGraph wire or the investigator loop: that is exercised live (README "Autonomous monitor")."""
import duckdb

from run_monitor import ring_alerts, risk_alerts


class FakeBackend:
    def __init__(self, rings, neighbors):
        self.rings, self.neighbors = rings, neighbors

    def find_rings(self, **kw):
        return self.rings

    def device_neighbors(self, device_profile, ts, days):
        return self.neighbors[device_profile]


def test_ring_alerts_picks_the_highest_total_unseen_card_per_device():
    backend = FakeBackend(
        rings=[{"device_profile": "DEV-A", "n_cards": 10}, {"device_profile": "DEV-B", "n_cards": 8}],
        neighbors={
            "DEV-A": {"window_cards": [{"card_id": "C1", "total": 500.0, "last_ts": "2016-12-01 00:00:00"},
                                       {"card_id": "C2", "total": 900.0, "last_ts": "2016-12-02 00:00:00"}]},
            "DEV-B": {"window_cards": [{"card_id": "C3", "total": 100.0, "last_ts": "2016-11-01 00:00:00"}]},
        })
    out = ring_alerts(backend, known_cards=set(), limit=5)
    assert [a["card_id"] for a in out] == ["C2", "C3"]           # C2 (900) beats C1 (500) on DEV-A; DEV-B has only C3
    assert all("ring device" in a["reason"] for a in out)


def test_ring_alerts_skips_cards_already_in_the_20_cases_and_respects_the_limit():
    backend = FakeBackend(
        rings=[{"device_profile": "DEV-A", "n_cards": 10}, {"device_profile": "DEV-B", "n_cards": 8}],
        neighbors={
            "DEV-A": {"window_cards": [{"card_id": "C1", "total": 900.0, "last_ts": "2016-12-01 00:00:00"}]},
            "DEV-B": {"window_cards": [{"card_id": "C3", "total": 100.0, "last_ts": "2016-11-01 00:00:00"}]},
        })
    out = ring_alerts(backend, known_cards={"C1"}, limit=1)
    assert [a["card_id"] for a in out] == ["C3"]                 # C1 excluded (already a benchmark case); limit=1 stops there


def _con():
    con = duckdb.connect(":memory:")
    con.execute("""CREATE TABLE feat (tid INTEGER, card_id VARCHAR, ts TIMESTAMP)""")
    con.execute("""CREATE TABLE tx (TransactionID INTEGER, risk_score DOUBLE)""")
    rows = [(1, "C1", "2016-12-01 00:00:00", 0.95), (2, "C1", "2016-12-02 00:00:00", 0.99),   # same card: only the top one counts
            (3, "C2", "2016-12-01 00:00:00", 0.90), (4, "C3", "2016-12-01 00:00:00", 0.10),
            (5, "C4", "2016-12-01 00:00:00", None)]                                            # no risk score: excluded
    for tid, card, ts, risk in rows:
        con.execute("INSERT INTO feat VALUES (?, ?, ?)", [tid, card, ts])
        con.execute("INSERT INTO tx VALUES (?, ?)", [tid, risk])
    return con


def test_risk_alerts_ranks_by_risk_excludes_known_and_dedupes_by_card():
    con = _con()
    out = risk_alerts(con, known_txn={3}, limit=5)               # C2's only transaction (tid 3) is a known benchmark alert
    assert [a["card_id"] for a in out] == ["C1", "C3"]            # C1 (via tid 2, risk .99) then C3 (.10); C2 excluded, C4 has no score
    assert out[0]["tid"] == 2                                     # the higher-risk of C1's two transactions


def test_risk_alerts_respects_the_limit():
    con = _con()
    out = risk_alerts(con, known_txn=set(), limit=1)
    assert len(out) == 1 and out[0]["card_id"] == "C1"
