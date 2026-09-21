"""Export the DuckDB feature store to load-ready CSVs for TigerGraph (data/store/tg/).

Missing numbers are written as -1 (TigerGraph has no NULL); agent/tg_backend.py maps -1 back to NaN.
Run after `python data/load_local.py`, `python -m agent.features`, `python -m agent.precompute` and `python -m agent.oof`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "store" / "graphsleuth.duckdb"
OUT = ROOT / "data" / "store" / "tg"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB), read_only=True)
    q = lambda name, sql: (con.execute(f"COPY ({sql}) TO '{OUT / name}' (HEADER, DELIMITER ',')"),
                           print(name, con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0], flush=True))

    q("cards.csv", "SELECT card_id, customer_id, coalesce(card4,'') card4, coalesce(card6,'') card6 FROM cards")
    q("devices.csv", """SELECT device_profile, count(DISTINCT card_id) n_cards, round(avg(dev_new), 4) new_frac, round(avg(proxy), 4) proxy_frac
                        FROM feat WHERE device_profile IS NOT NULL GROUP BY 1""")
    q("regions.csv", "SELECT DISTINCT CAST(addr1 AS INT) addr1 FROM feat WHERE addr1 IS NOT NULL")
    q("emails.csv", "SELECT DISTINCT P_emaildomain AS email_domain FROM tx WHERE P_emaildomain IS NOT NULL")
    n = lambda c: f"coalesce(CAST({c} AS DOUBLE), -1)"
    q("transactions.csv", f"""
        SELECT f.tid, strftime(f.ts, '%Y-%m-%d %H:%M:%S') ts, f.amt, f.prod, f.channel, {n('f.addr1')} addr1, t.risk_score bank_risk,
               {n('s.p')} p, {n('o.p')} p_oof,
               CAST({n('f.dev_new')} AS INT) dev_new, CAST({n('f.dev_unseen')} AS INT) dev_unseen, CAST({n('f.addr1_new')} AS INT) addr1_new,
               CAST({n('f.prod_new')} AS INT) prod_new, f.proxy AS via_proxy, {n('f.amt_ratio_med')} amt_ratio_med, {n('f.gap_prev_h')} gap_prev_h,
               f.n_1h, f.n_small_1h, f.addr1_prior_n, f.prior_n,
               {n('s.pat_account_takeover')} pat_ato, {n('s.pat_card_not_present_fraud')} pat_cnp,
               {n('s.pat_card_not_present_new_device')} pat_cnpnd, {n('s.pat_out_of_region_use')} pat_oor, {n('s.pat_none')} pat_none,
               f.card_id, coalesce(f.device_profile, '') device_profile, coalesce(t.P_emaildomain, '') email,
               coalesce(CAST(CAST(f.addr1 AS INT) AS VARCHAR), '') region,
               coalesce(CAST(lead(f.tid) OVER (PARTITION BY f.card_id ORDER BY f.ts) AS VARCHAR), '') next_tid
        FROM feat f JOIN tx t ON t.TransactionID = f.tid LEFT JOIN scores_final s ON s.tid = f.tid LEFT JOIN scores_oof o ON o.tid = f.tid""")
    q("closed_cases.csv", """SELECT case_id, outcome, pattern, exposure_usd, strftime(opened_at, '%Y-%m-%d %H:%M:%S') opened_at,
                                    strftime(closed_at, '%Y-%m-%d %H:%M:%S') closed_at, report_filed, replace(analyst_notes, '"', '''') notes, card_id
                             FROM closed_cases""")
    q("closed_case_txn.csv", "SELECT case_id, CAST(u.x AS BIGINT) tid FROM closed_cases, unnest(string_split(txn_ids, '|')) u(x) WHERE txn_ids <> ''")
    q("closed_case_conn.csv", "SELECT case_id, u.x card_id FROM closed_cases, unnest(string_split(connected_card_ids, '|')) u(x) WHERE connected_card_ids IS NOT NULL AND connected_card_ids <> ''")
    print("exported to", OUT)


if __name__ == "__main__":
    main()
