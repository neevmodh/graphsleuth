"""Graph-access layer for the agent.

`LocalBackend` answers every tool call from the local DuckDB store. `TigerGraphBackend`
(agent/tg_backend.py) implements the same interface with installed GSQL queries over the same schema.
Each method mirrors one installed query and returns compact evidence, never raw rows.
"""
from __future__ import annotations

import re
from typing import Protocol

import duckdb
import pandas as pd

from .scorer import DB

_SAFE = re.compile(r"^[A-Za-z0-9_\-\.\| :/()+,=\?']*$")


def _q(value: str) -> str:
    """SQL string literal with quotes escaped (device profiles contain no quotes, but be safe)."""
    return "'" + str(value).replace("'", "''") + "'"


class GraphBackend(Protocol):
    def transaction(self, tid: int) -> dict: ...
    def card_profile(self, card_id: str, before_ts: str) -> dict: ...
    def card_window(self, card_id: str, ts: str, hours_before: float, hours_after: float) -> pd.DataFrame: ...
    def device_neighbors(self, device_profile: str, ts: str, days: int) -> dict: ...
    def email_history(self, card_id: str, email: str, ts: str) -> dict: ...
    def region_history(self, card_id: str, addr1: float, ts: str) -> dict: ...
    def region_cluster(self, addr1: float, ts: str, days: int) -> dict: ...
    def recurring_check(self, card_id: str, tid: int) -> dict: ...
    def customer_cards(self, customer_id: str) -> list[str]: ...
    def similar_cases(self, **kw) -> list[dict]: ...


class LocalBackend:
    def __init__(self, variant: str = "final", db: str | None = None, read_only: bool = True):
        self.variant = variant           # 'dev' (leak-free for October tests) | 'final' (exam)
        self.con = duckdb.connect(str(db or DB), read_only=read_only)
        self.scores = f"scores_{variant}"

    # ---- transactions ---------------------------------------------------------------------------
    def transaction(self, tid: int) -> dict:
        df = self.con.execute(f"""
            SELECT f.*, t.risk_score AS bank_risk, t.P_emaildomain, t.R_emaildomain, t.card4, t.card6,
                   s.p, s.p_raw, s.pat_account_takeover, s.pat_card_not_present_fraud,
                   s.pat_card_not_present_new_device, s.pat_out_of_region_use, s.pat_none
            FROM feat f JOIN tx t ON t.TransactionID = f.tid LEFT JOIN {self.scores} s ON s.tid = f.tid
            WHERE f.tid = {int(tid)}""").df()
        if df.empty:
            raise KeyError(f"transaction {tid} not found")
        return _clean(df.iloc[0].to_dict())

    def card_profile(self, card_id: str, before_ts: str) -> dict:
        r = self.con.execute(f"""
            SELECT count(*) n, min(ts) first_ts, avg(online) online_frac, median(amt) med_amt, avg(amt) mean_amt,
                   max(amt) max_amt, count(DISTINCT addr1) regions, count(DISTINCT device_profile) devices,
                   count(DISTINCT prod) products
            FROM feat WHERE card_id = {_q(card_id)} AND ts < {_q(before_ts)}""").fetchone()
        top = self.con.execute(f"""
            SELECT addr1, count(*) c FROM feat WHERE card_id = {_q(card_id)} AND ts < {_q(before_ts)} AND addr1 IS NOT NULL
            GROUP BY 1 ORDER BY c DESC LIMIT 3""").fetchall()
        keys = ["n_prior", "first_ts", "online_frac", "median_amt", "mean_amt", "max_amt", "regions", "devices", "products"]
        d = dict(zip(keys, r, strict=True))
        d["top_regions"] = [{"addr1": a, "n": c} for a, c in top]
        d["home_region"] = top[0][0] if top else None
        return _clean(d)

    def card_window(self, card_id: str, ts: str, hours_before: float, hours_after: float) -> pd.DataFrame:
        df = self.con.execute(f"""
            SELECT f.tid, f.ts, f.amt, f.prod, f.channel, f.addr1, f.device_profile, f.dev_new, f.proxy, f.amt_ratio_med,
                   f.gap_prev_h, f.prior_n, f.n_1h, f.n_small_1h, f.addr1_prior_n, f.addr1_new, f.prod_new, f.dev_unseen,
                   t.risk_score AS bank_risk, s.p, o.p AS p_oof, s.pat_account_takeover, s.pat_card_not_present_fraud,
                   s.pat_card_not_present_new_device, s.pat_out_of_region_use, s.pat_none
            FROM feat f JOIN tx t ON t.TransactionID = f.tid LEFT JOIN {self.scores} s ON s.tid = f.tid
                 LEFT JOIN scores_oof o ON o.tid = f.tid
            WHERE f.card_id = {_q(card_id)}
              AND f.ts BETWEEN {_q(ts)}::TIMESTAMP - to_seconds({float(hours_before) * 3600}) AND {_q(ts)}::TIMESTAMP + to_seconds({float(hours_after) * 3600})
            ORDER BY f.ts""").df()
        return df

    # ---- entity links ---------------------------------------------------------------------------
    def device_neighbors(self, device_profile: str, ts: str, days: int = 14) -> dict:
        g = self.con.execute(f"""
            SELECT count(*) txns, count(DISTINCT card_id) cards, avg(dev_new) new_frac, avg(proxy) proxy_frac, min(ts) first_ts, max(ts) last_ts
            FROM feat WHERE device_profile = {_q(device_profile)}""").fetchone()
        win = self.con.execute(f"""
            SELECT card_id, count(*) n, min(ts) first_ts, max(ts) last_ts, sum(amt) total, avg(p) avg_p
            FROM (SELECT f.card_id, f.ts, f.amt, s.p FROM feat f LEFT JOIN {self.scores} s ON s.tid = f.tid
                  WHERE f.device_profile = {_q(device_profile)}
                    AND f.ts BETWEEN {_q(ts)}::TIMESTAMP - to_days({int(days)}) AND {_q(ts)}::TIMESTAMP + to_days({int(days)}))
            GROUP BY 1 ORDER BY n DESC""").df()
        cases = self.con.execute(f"""
            SELECT DISTINCT c.case_id, c.card_id, c.outcome, c.pattern, c.opened_at
            FROM closed_cases c, unnest(string_split(c.txn_ids, '|')) u(x)
            JOIN feat f ON f.tid = CAST(u.x AS BIGINT)
            WHERE f.device_profile = {_q(device_profile)} AND c.txn_ids <> '' ORDER BY c.opened_at""").df()
        return _clean({
            "device_profile": device_profile, "txns": g[0], "cards": g[1], "new_frac": g[2], "proxy_frac": g[3],
            "first_ts": g[4], "last_ts": g[5],
            "window_cards": win.to_dict("records"),
            "closed_cases": cases.to_dict("records"),
        })

    def device_burst(self, device_profile: str, ts: str, max_gap_days: float = 7.0) -> dict:
        """Cards active on a device in the same burst (consecutive device activity with gaps <= max_gap_days)
        as the transaction at `ts`. This is the ring the bank's analysts listed as 'connected cards'."""
        df = self.con.execute(f"""
            SELECT f.ts, f.card_id, f.tid, f.amt, s.p FROM feat f LEFT JOIN {self.scores} s ON s.tid = f.tid
            WHERE f.device_profile = {_q(device_profile)} ORDER BY f.ts""").df()
        if df.empty:
            return {"cards": [], "txns": 0}
        t0 = pd.Timestamp(ts)
        gap = df["ts"].diff().dt.total_seconds().fillna(0) / 86400
        df["burst"] = (gap > max_gap_days).cumsum()
        near = df.iloc[(df["ts"] - t0).abs().argmin()]["burst"]
        b = df[df["burst"] == near]
        per = b.groupby("card_id").agg(n=("tid", "count"), total=("amt", "sum"), first_ts=("ts", "min"), last_ts=("ts", "max"),
                                       tids=("tid", list)).reset_index()
        return _clean({"burst_start": b["ts"].min(), "burst_end": b["ts"].max(), "txns": len(b), "cards": per.to_dict("records")})

    def email_history(self, card_id: str, email: str, ts: str) -> dict:
        """Has this card used this purchaser email domain before, and how many distinct domains has it used? Only 59
        distinct domains exist in the whole dataset (mostly gmail.com/yahoo.com/...), so raw sharing across cards is
        meaningless -- the signal is per-card consistency, the same idea as dev_unseen for devices."""
        r = self.con.execute(f"""
            WITH hx AS (
                SELECT f.ts, t.P_emaildomain AS dom FROM feat f JOIN tx t ON t.TransactionID = f.tid
                WHERE f.card_id = {_q(card_id)} AND f.ts < {_q(ts)} AND t.P_emaildomain IS NOT NULL AND t.P_emaildomain <> ''
            )
            SELECT count(*) FILTER (WHERE dom = {_q(email)}) n_before,
                   min(ts) FILTER (WHERE dom = {_q(email)}) first_seen,
                   count(DISTINCT dom) n_distinct_domains_before
            FROM hx""").fetchone()
        return _clean({"n_before": r[0], "first_seen": r[1] if r[0] else None, "n_distinct_domains_before": r[2]})

    def region_history(self, card_id: str, addr1: float, ts: str) -> dict:
        r = self.con.execute(f"""
            SELECT count(*) n_before, min(ts) first_seen FROM feat WHERE card_id = {_q(card_id)} AND addr1 = {float(addr1)} AND ts < {_q(ts)}""").fetchone()
        near = self.con.execute(f"""
            SELECT count(*) n, count(DISTINCT CAST(ts AS DATE)) n_days,
                   count(*) FILTER (WHERE addr1 = {float(addr1)}) in_region,
                   count(*) FILTER (WHERE addr1 <> {float(addr1)} AND addr1 IS NOT NULL) elsewhere
            FROM feat WHERE card_id = {_q(card_id)} AND ts BETWEEN {_q(ts)}::TIMESTAMP - to_days(5) AND {_q(ts)}::TIMESTAMP + to_days(5)""").fetchone()
        days_in = self.con.execute(f"""
            SELECT count(DISTINCT CAST(ts AS DATE)) FROM feat WHERE card_id = {_q(card_id)} AND addr1 = {float(addr1)}
              AND ts BETWEEN {_q(ts)}::TIMESTAMP - to_days(5) AND {_q(ts)}::TIMESTAMP + to_days(5)""").fetchone()[0]
        return _clean({"addr1": addr1, "n_before": r[0], "first_seen": r[1], "n_near": near[0],
                       "in_region_near": near[2], "elsewhere_near": near[3], "days_in_region_near": days_in})

    def region_cluster(self, addr1: float, ts: str, days: int = 7) -> dict:
        r = self.con.execute(f"""
            SELECT count(*) txns, count(DISTINCT f.card_id) cards, avg(s.p) avg_p, sum((s.p >= 0.5)::INT) likely_fraud
            FROM feat f LEFT JOIN {self.scores} s ON s.tid = f.tid
            WHERE f.addr1 = {float(addr1)} AND f.ts BETWEEN {_q(ts)}::TIMESTAMP - to_days({int(days)}) AND {_q(ts)}::TIMESTAMP + to_days({int(days)})""").fetchone()
        return _clean({"addr1": addr1, "txns": r[0], "cards": r[1], "avg_p": r[2], "likely_fraud_txns": r[3]})

    def recurring_check(self, card_id: str, tid: int) -> dict:
        """Is this charge a subscription-like recurrence on the card? There is no merchant field, so the proxy is:
        same product, identical amount (+/- 1 cent), earlier occurrences at monthly gaps (26-34 days),
        and a LOW frequency. The frequency guard matters: a popular price point (e.g. $39.08 seen 32 times in
        five months) repeats often by chance and is not a subscription."""
        rows = self.con.execute(f"""
            WITH t0 AS (SELECT ts, amt, prod FROM feat WHERE tid = {int(tid)})
            SELECT f.ts, (f.tid = {int(tid)}) AS is_self FROM feat f, t0
            WHERE f.card_id = {_q(card_id)} AND f.prod = t0.prod AND abs(f.amt - t0.amt) <= 0.011 AND f.ts <= t0.ts
            ORDER BY f.ts""").fetchall()
        times = [r[0] for r in rows]
        n_prior = max(len(times) - 1, 0)
        gaps = [(b - a).total_seconds() / 86400 for a, b in zip(times, times[1:], strict=False)]
        mid = [g for g in gaps if 26 <= g <= 34]
        span = (times[-1] - times[0]).total_seconds() / 86400 if len(times) > 1 else 0.0
        rate30 = n_prior / (max(span, 30.0) / 30.0)
        return {"n_same_amount": len(times), "n_prior": n_prior, "monthly_gaps": len(mid), "gaps_days": [round(g, 1) for g in gaps[-6:]],
                "rate_per_30d": round(rate30, 2), "is_recurring": n_prior >= 2 and len(mid) >= 2 and rate30 <= 1.6}

    def customer_cards(self, customer_id: str) -> list[str]:
        return [r[0] for r in self.con.execute(
            f"SELECT card_id FROM cards WHERE customer_id = {_q(customer_id)} ORDER BY card_id").fetchall()]

    # ---- memory ---------------------------------------------------------------------------------
    def similar_cases(self, *, card_id: str, device_profile: str | None, addr1: float | None, pattern: str | None,
                      before_ts: str, exclude_case: str | None = None, k: int = 5) -> list[dict]:
        """Closed cases most relevant to this alert (structural retrieval; the GraphRAG layer adds
        vector similarity over the case narratives on top of this)."""
        dev = _q(device_profile) if isinstance(device_profile, str) and device_profile else "NULL"   # NaN is truthy: test the type
        reg = float(addr1) if addr1 is not None and addr1 == addr1 else "NULL"
        df = self.con.execute(f"""
            WITH ct AS (SELECT c.case_id, c.card_id, c.outcome, c.pattern, c.exposure_usd, c.opened_at,
                               CAST(u.x AS BIGINT) tid
                        FROM closed_cases c, unnest(string_split(c.txn_ids, '|')) u(x) WHERE c.txn_ids <> ''
                          AND c.closed_at < {_q(before_ts)} AND c.case_id <> {_q(exclude_case or '')}),
            sc AS (SELECT ct.case_id, any_value(ct.card_id) card, any_value(ct.outcome) outcome, any_value(ct.pattern) pattern,
                          any_value(ct.exposure_usd) exposure, any_value(ct.opened_at) opened_at,
                          -- flags default to 0: a comparison with a NULL device/region is NULL, and NULL would silently drop the row
                          coalesce(max((f.device_profile = {dev} AND f.dev_cards <= 60)::INT), 0) same_device,   -- a device on hundreds of cards links nothing
                          coalesce(max((f.addr1 = {reg})::INT), 0) same_region,
                          coalesce(max((ct.card_id = {_q(card_id)})::INT), 0) same_card,
                          coalesce(max(({_q(pattern or '')} <> '' AND ct.pattern = {_q(pattern or '')})::INT), 0) same_pattern
                   FROM ct JOIN feat f ON f.tid = ct.tid GROUP BY ct.case_id)
            SELECT *, 3 * same_device + 3 * same_card + same_region + 2 * same_pattern AS score
            FROM sc WHERE same_device + same_card + same_region > 0   -- an entity link is required; the pattern only boosts rank
            ORDER BY score DESC, opened_at DESC LIMIT {int(k)}""").df()
        return _clean(df.to_dict("records"))

    # ---- lookups used by the evaluation harness ------------------------------------------------------
    def query(self, sql: str) -> pd.DataFrame:
        return self.con.execute(sql).df()


def _clean(obj):
    """Make DuckDB/pandas values JSON-friendly (NaN/NaT -> None, numpy -> python, timestamps -> str)."""
    import math
    import numpy as np
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        return None if (obj != obj or math.isinf(obj)) else float(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return None if pd.isna(obj) else obj.strftime("%Y-%m-%d %H:%M:%S")
    if obj is pd.NaT or obj is pd.NA:
        return None
    if hasattr(obj, "isoformat"):
        return str(obj)
    return obj
