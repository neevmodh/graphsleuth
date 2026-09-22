"""Behavioural features per transaction, materialised as the `feat` table in DuckDB.

Every history feature uses only transactions strictly *before* the row's own timestamp on the same
card (no leakage of the future), except the explicit `gap_next_h` used for episode grouping.
The same logic is expressed as GSQL in graph/queries for the TigerGraph backend.
"""
from __future__ import annotations

import duckdb

FEATURE_SQL = """
CREATE OR REPLACE TABLE feat AS
WITH base AS (
  SELECT TransactionID AS tid, card_id, customer_id, ts, epoch(ts) AS e, TransactionAmt AS amt,
         ProductCD AS prod, channel, addr1, addr2, P_emaildomain AS pem, device_profile,
         (id_15 = 'New')::INT AS dev_new, (id_23 IS NOT NULL)::INT AS proxy,
         (channel = 'online')::INT AS online, risk_score
  FROM tx
),
w AS (
  SELECT *,
    row_number() OVER c                                    AS prior_n0,
    e - lag(e) OVER c                                      AS gap_prev_s,
    lead(e) OVER c - e                                     AS gap_next_s,
    count(*) OVER (PARTITION BY card_id ORDER BY e RANGE BETWEEN 3600 PRECEDING AND CURRENT ROW)        AS n_1h,
    count(*) OVER (PARTITION BY card_id ORDER BY e RANGE BETWEEN 86400 PRECEDING AND CURRENT ROW)       AS n_24h,
    count(*) OVER (PARTITION BY card_id ORDER BY e RANGE BETWEEN 172800 PRECEDING AND CURRENT ROW)      AS n_48h,
    count(*) OVER (PARTITION BY card_id ORDER BY e RANGE BETWEEN 3600 PRECEDING AND CURRENT ROW)
        - count(*) FILTER (WHERE amt >= 5) OVER (PARTITION BY card_id ORDER BY e RANGE BETWEEN 3600 PRECEDING AND CURRENT ROW) AS n_small_1h,
    count(*) FILTER (WHERE online = 1) OVER (PARTITION BY card_id ORDER BY e RANGE BETWEEN 172800 PRECEDING AND CURRENT ROW) AS n_online_48h,
    count(*) FILTER (WHERE online = 0) OVER (PARTITION BY card_id ORDER BY e RANGE BETWEEN 172800 PRECEDING AND CURRENT ROW) AS n_inperson_48h,
    count(*) OVER (PARTITION BY card_id, addr1 ORDER BY e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)  AS addr1_prior_n,
    count(*) OVER (PARTITION BY card_id, prod ORDER BY e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)   AS prod_prior_n,
    count(*) OVER (PARTITION BY card_id, device_profile ORDER BY e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS dev_prior_n,
    count(*) OVER (PARTITION BY card_id, pem ORDER BY e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)   AS pem_prior_n,
    count(*) OVER (PARTITION BY card_id, channel ORDER BY e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS chan_prior_n,
    count(addr1) OVER (PARTITION BY card_id ORDER BY e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)    AS addr1_known_prior,
    avg(amt) OVER (PARTITION BY card_id ORDER BY e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)        AS amt_prior_mean,
    stddev_samp(amt) OVER (PARTITION BY card_id ORDER BY e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS amt_prior_sd,
    median(amt) OVER (PARTITION BY card_id ORDER BY e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)    AS amt_prior_med,
    avg(online) OVER (PARTITION BY card_id ORDER BY e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)    AS online_prior_frac,
    min(e) OVER (PARTITION BY card_id)                                                                      AS first_e
  FROM base
  WINDOW c AS (PARTITION BY card_id ORDER BY e)
),
dev AS (   -- global device structure (rarity), not label-derived
  SELECT device_profile, count(*) AS dev_txns, count(DISTINCT card_id) AS dev_cards,
         avg(dev_new) AS dev_new_frac, avg(proxy) AS dev_proxy_frac
  FROM base WHERE device_profile IS NOT NULL GROUP BY 1
),
reg AS (   -- billing-region cluster size
  SELECT addr1, count(DISTINCT card_id) AS reg_cards, count(*) AS reg_txns FROM base WHERE addr1 IS NOT NULL GROUP BY 1
)
SELECT w.tid, w.card_id, w.customer_id, w.ts, w.amt, w.prod, w.channel, w.addr1, w.addr2, w.device_profile,
       w.online, w.dev_new, w.proxy, w.risk_score,
       w.prior_n0 - 1 AS prior_n,
       (w.e - w.first_e) / 86400.0 AS card_age_d,
       w.gap_prev_s / 3600.0 AS gap_prev_h, w.gap_next_s / 3600.0 AS gap_next_h,
       w.n_1h, w.n_24h, w.n_48h, w.n_small_1h, w.n_online_48h, w.n_inperson_48h,
       w.addr1_prior_n, w.addr1_known_prior,
       CASE WHEN w.addr1 IS NULL THEN NULL WHEN w.addr1_prior_n = 0 THEN 1 ELSE 0 END AS addr1_new,
       w.prod_prior_n, (w.prod_prior_n = 0)::INT AS prod_new,
       w.dev_prior_n, CASE WHEN w.device_profile IS NULL THEN NULL WHEN w.dev_prior_n = 0 THEN 1 ELSE 0 END AS dev_unseen,
       w.pem_prior_n, (w.chan_prior_n = 0)::INT AS chan_new,
       w.amt_prior_mean, w.amt_prior_sd, w.amt_prior_med,
       w.amt / nullif(w.amt_prior_med, 0) AS amt_ratio_med,
       (w.amt - w.amt_prior_mean) / nullif(w.amt_prior_sd, 0) AS amt_z,
       w.online_prior_frac,
       ln(1 + d.dev_cards) AS dev_log_cards, d.dev_txns, d.dev_cards, d.dev_new_frac, d.dev_proxy_frac,
       r.reg_cards, r.reg_txns,
       hour(w.ts) AS hod, dayofweek(w.ts) AS dow
FROM w
LEFT JOIN dev d USING (device_profile)
LEFT JOIN reg r USING (addr1)
"""


def build(con: duckdb.DuckDBPyConnection) -> int:
    con.execute("PRAGMA memory_limit='6GB'")
    con.execute(FEATURE_SQL)
    con.execute("CREATE INDEX IF NOT EXISTS idx_feat_tid ON feat(tid)")
    return con.execute("SELECT count(*) FROM feat").fetchone()[0]


if __name__ == "__main__":
    from pathlib import Path
    db = Path(__file__).resolve().parents[1] / "data" / "store" / "graphsleuth.duckdb"
    con = duckdb.connect(str(db))
    print("feat rows:", f"{build(con):,}")
