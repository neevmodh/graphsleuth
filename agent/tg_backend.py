"""TigerGraph backend: the same interface as `LocalBackend`, answered by the installed GSQL queries in graph/queries.gsql.

STATUS: the queries are type-checked against a real TigerGraph 4.2.5 schema, but this class has NOT yet been run against a
live instance (no Savanna workspace was available; local Community Edition does not run on Apple Silicon). Response parsing
follows pyTigerGraph's documented shape (one dict per PRINT; vertex sets as {"v_id", "v_type", "attributes"}), and is
unit-tested against a fake connection only. Run tests/test_tg_live.py once with TG_HOST set to verify against the wire.
"""
from __future__ import annotations

import math
import os
import time
from datetime import timedelta

import pandas as pd
from dotenv import load_dotenv

from .backend import _clean
from .schemas import Case
from .scorer import ROOT

load_dotenv(ROOT / ".env")

SENTINEL = -1   # TigerGraph has no NULL; the exporter writes -1 for "unknown" numbers


def _num(x):
    """-1 sentinel -> None; leave real values untouched."""
    return None if x is None or (isinstance(x, (int, float)) and x == SENTINEL) else x


def _attrs(vertex_list) -> list[dict]:
    return [v["attributes"] for v in (vertex_list or [])]


def _merge(results: list[dict]) -> dict:
    """pyTigerGraph returns one dict per PRINT; merge them into one lookup."""
    out: dict = {}
    for r in results or []:
        out.update(r)
    return out


def connect():
    """Open a connection from the environment (see .env.example): TG_HOST, TG_GRAPH, TG_SECRET or TG_USERNAME/TG_PASSWORD."""
    from pyTigerGraph import TigerGraphConnection
    host, graph = os.environ["TG_HOST"], os.environ.get("TG_GRAPH", "GraphSleuth")
    secret = os.environ.get("TG_SECRET")
    kw = dict(host=host, graphname=graph, tgCloud="tgcloud.io" in host)
    if secret:
        conn = TigerGraphConnection(gsqlSecret=secret, **kw)
        conn.getToken(secret)
    else:
        conn = TigerGraphConnection(username=os.environ.get("TG_USERNAME", "tigergraph"), password=os.environ["TG_PASSWORD"], **kw)
        conn.getToken(conn.createSecret())
    return conn


class TigerGraphBackend:
    """Evidence tools over TigerGraph. `variant` is kept for orchestrator compatibility (models trained on all labelled months)."""

    variant = "final"

    def __init__(self, conn=None, timeout: int = 60):
        self.conn = conn or connect()
        self.timeout = timeout

    # ---- plumbing ---------------------------------------------------------------------------------------------
    def _run(self, name: str, **params) -> dict:
        res = self.conn.runInstalledQuery(name, params=params, timeout=self.timeout * 1000, sizeLimit=200_000_000)
        return _merge(res)

    @staticmethod
    def _txn_row(a: dict, dev: str | None = None) -> dict:
        """Transaction attributes -> the column names the investigator and models use (NaN for the -1 sentinel)."""
        g = lambda k: _num(a.get(k))
        row = {
            "tid": int(a["tid"]), "ts": pd.Timestamp(a["ts"]), "amt": a["amt"], "prod": a["prod"], "channel": a["channel"],
            "addr1": g("addr1"), "bank_risk": a["bank_risk"], "p": g("p"), "p_oof": g("p_oof"),
            "dev_new": g("dev_new"), "dev_unseen": g("dev_unseen"), "addr1_new": g("addr1_new"), "prod_new": g("prod_new"),
            "proxy": a.get("via_proxy"), "amt_ratio_med": g("amt_ratio_med"), "gap_prev_h": g("gap_prev_h"),
            "n_1h": a.get("n_1h"), "n_small_1h": a.get("n_small_1h"), "addr1_prior_n": a.get("addr1_prior_n"), "prior_n": a.get("prior_n"),
            "pat_account_takeover": g("pat_ato"), "pat_card_not_present_fraud": g("pat_cnp"),
            "pat_card_not_present_new_device": g("pat_cnpnd"), "pat_out_of_region_use": g("pat_oor"), "pat_none": g("pat_none"),
            "device_profile": dev,
        }
        for k in ("addr1", "dev_new", "dev_unseen", "addr1_new", "prod_new", "amt_ratio_med", "gap_prev_h", "p", "p_oof"):
            if row[k] is None:
                row[k] = float("nan")
        return row

    # ---- transactions -----------------------------------------------------------------------------------------
    def transaction(self, tid: int) -> dict:
        r = self._run("get_transaction", txn=(str(int(tid)), "Transaction"))
        rows = _attrs(r.get("Start"))
        if not rows:
            raise KeyError(f"transaction {tid} not found")
        a = rows[0]
        dev = (r.get("device_by_tid") or {}).get(str(int(tid)))
        row = self._txn_row(a, dev)
        row["card_id"] = a.get("card_id")
        card = self.conn.getVerticesById("Card", a.get("card_id"))
        if card:
            c = card[0]["attributes"] if isinstance(card, list) else card.get("attributes", {})
            row["card4"], row["card6"] = c.get("card4") or None, c.get("card6") or None
        return _clean(row)

    def card_profile(self, card_id: str, before_ts: str) -> dict:
        r = self._run("card_profile", card=(card_id, "Card"), before=before_ts)
        n = r.get("n", 0) or 0
        amts = sorted(r.get("amts") or [])
        med = (amts[len(amts) // 2] if len(amts) % 2 else (amts[len(amts) // 2 - 1] + amts[len(amts) // 2]) / 2) if amts else None
        regions = {int(k): v for k, v in (r.get("regions") or {}).items()}
        top = sorted(regions.items(), key=lambda kv: -kv[1])[:3]
        return _clean({
            "n_prior": n, "first_ts": r.get("first_ts") if n else None, "online_frac": (r.get("n_online", 0) / n) if n else None,
            "median_amt": med, "mean_amt": (r.get("sum_amt", 0) / n) if n else None, "max_amt": r.get("max_amt") if n else None,
            "regions": len(regions), "devices": len(r.get("devices") or []), "products": len(r.get("prods") or []),
            "top_regions": [{"addr1": float(a), "n": c} for a, c in top], "home_region": float(top[0][0]) if top else None})

    def card_window(self, card_id: str, ts: str, hours_before: float, hours_after: float) -> pd.DataFrame:
        t0 = pd.Timestamp(ts)
        r = self._run("card_window", card=(card_id, "Card"),
                      t_from=str(t0 - timedelta(hours=hours_before)), t_to=str(t0 + timedelta(hours=hours_after)))
        dev = r.get("device_by_tid") or {}
        rows = [self._txn_row(a, dev.get(str(a["tid"]))) for a in _attrs(r.get("T"))]
        cols = ["tid", "ts", "amt", "prod", "channel", "addr1", "device_profile", "dev_new", "proxy", "amt_ratio_med", "gap_prev_h",
                "prior_n", "n_1h", "n_small_1h", "addr1_prior_n", "addr1_new", "prod_new", "dev_unseen", "bank_risk", "p", "p_oof",
                "pat_account_takeover", "pat_card_not_present_fraud", "pat_card_not_present_new_device", "pat_out_of_region_use", "pat_none"]
        df = pd.DataFrame(rows, columns=cols)
        return df.sort_values("ts").reset_index(drop=True)

    # ---- entity links -----------------------------------------------------------------------------------------
    def device_neighbors(self, device_profile: str, ts: str, days: int = 14) -> dict:
        t0 = pd.Timestamp(ts)
        r = self._run("device_neighbors", dev=(device_profile, "DeviceProfile"),
                      t_from=str(t0 - timedelta(days=days)), t_to=str(t0 + timedelta(days=days)))
        head = (_attrs(r.get("Start")) or [{}])[0]
        win = [{"card_id": c, "n": n, "first_ts": r["window_first"][c], "last_ts": r["window_last"][c], "total": r["window_total"][c]}
               for c, n in (r.get("window_n") or {}).items()]
        cases = [{"case_id": a["case_id"], "outcome": a["outcome"], "pattern": a["pattern"], "opened_at": a["opened_at"]}
                 for a in _attrs(r.get("C"))]
        return _clean({"device_profile": device_profile, "txns": head.get("txns", 0), "cards": head.get("n_cards", 0),
                       "new_frac": head.get("new_frac"), "proxy_frac": head.get("proxy_frac"),
                       "first_ts": r.get("first_ts"), "last_ts": r.get("last_ts"), "window_cards": win, "closed_cases": cases})

    def device_burst(self, device_profile: str, ts: str, max_gap_days: float = 7.0) -> dict:
        r = self._run("device_txns", dev=(device_profile, "DeviceProfile"))
        rows = r.get("rows") or []
        if not rows:
            return {"cards": [], "txns": 0}
        df = pd.DataFrame(rows)
        df["ts"] = pd.to_datetime(df["ts"])
        df = df.rename(columns={"card": "card_id"}).sort_values("ts").reset_index(drop=True)
        gap = df["ts"].diff().dt.total_seconds().fillna(0) / 86400
        df["burst"] = (gap > max_gap_days).cumsum()
        near = df.iloc[(df["ts"] - pd.Timestamp(ts)).abs().argmin()]["burst"]
        b = df[df["burst"] == near]
        per = b.groupby("card_id").agg(n=("tid", "count"), total=("amt", "sum"), first_ts=("ts", "min"), last_ts=("ts", "max"),
                                       tids=("tid", list)).reset_index()
        return _clean({"burst_start": b["ts"].min(), "burst_end": b["ts"].max(), "txns": len(b), "cards": per.to_dict("records")})

    def region_history(self, card_id: str, addr1: float, ts: str) -> dict:
        t0 = pd.Timestamp(ts)
        r = self._run("region_history", card=(card_id, "Card"), addr1=float(addr1), ts=str(t0),
                      t_lo=str(t0 - timedelta(days=5)), t_hi=str(t0 + timedelta(days=5)))
        return _clean({"addr1": addr1, "n_before": r.get("n_before", 0), "first_seen": r.get("first_seen") if r.get("n_before") else None,
                       "n_near": r.get("n_near", 0), "in_region_near": r.get("in_region", 0), "elsewhere_near": r.get("elsewhere", 0),
                       "days_in_region_near": r.get("days_in_region", 0)})

    def region_cluster(self, addr1: float, ts: str, days: int = 7) -> dict:
        t0 = pd.Timestamp(ts)
        r = self._run("region_cluster", region=(str(int(addr1)), "BillingRegion"),
                      t_from=str(t0 - timedelta(days=days)), t_to=str(t0 + timedelta(days=days)))
        return _clean({"addr1": addr1, "txns": r.get("txns", 0), "cards": r.get("cards", 0), "avg_p": r.get("avg_p"),
                       "likely_fraud_txns": r.get("likely_fraud", 0)})

    def recurring_check(self, card_id: str, tid: int) -> dict:
        """Same rule as LocalBackend.recurring_check: identical amount+product, monthly gaps (26-34 d), low frequency."""
        tx = self.transaction(tid)
        r = self._run("recurring_txns", card=(card_id, "Card"), prod=tx["prod"], amt=float(tx["amt"]), upto=str(tx["ts"]))
        times = sorted(pd.Timestamp(t) for t in (r.get("ts") or []))
        n_prior = max(len(times) - 1, 0)
        gaps = [(b - a).total_seconds() / 86400 for a, b in zip(times, times[1:])]
        mid = [g for g in gaps if 26 <= g <= 34]
        span = (times[-1] - times[0]).total_seconds() / 86400 if len(times) > 1 else 0.0
        rate30 = n_prior / (max(span, 30.0) / 30.0)
        return {"n_same_amount": len(times), "n_prior": n_prior, "monthly_gaps": len(mid), "gaps_days": [round(g, 1) for g in gaps[-6:]],
                "rate_per_30d": round(rate30, 2), "is_recurring": n_prior >= 2 and len(mid) >= 2 and rate30 <= 1.6}

    def customer_cards(self, customer_id: str) -> list[str]:
        r = self._run("customer_cards", cust=(customer_id, "Customer"))
        return sorted(a["card_id"] for a in _attrs(r.get("C")))

    # ---- memory -----------------------------------------------------------------------------------------------
    def similar_cases(self, *, card_id: str, device_profile: str | None, addr1: float | None, pattern: str | None,
                      before_ts: str, exclude_case: str | None = None, k: int = 5) -> list[dict]:
        found: dict[str, dict] = {}

        def add(rows, flag):
            for a in _attrs(rows):
                d = found.setdefault(a["case_id"], {"case_id": a["case_id"], "outcome": a["outcome"], "pattern": a["pattern"],
                                                    "exposure": a["exposure"], "opened_at": a["opened_at"],
                                                    "same_card": 0, "same_device": 0, "same_region": 0})
                d[flag] = 1

        add(self._run("closed_cases_card", card=(card_id, "Card"), before=before_ts).get("A"), "same_card")
        if device_profile:
            add(self._run("closed_cases_device", dev=(device_profile, "DeviceProfile"), before=before_ts, max_dev_cards=60).get("B"), "same_device")
        if addr1 is not None and addr1 == addr1:
            add(self._run("closed_cases_region", region=(str(int(addr1)), "BillingRegion"), before=before_ts).get("C"), "same_region")
        out = []
        for d in found.values():
            if d["case_id"] == exclude_case:
                continue
            d["same_pattern"] = int(bool(pattern) and d["pattern"] == pattern)
            d["score"] = 3 * d["same_device"] + 3 * d["same_card"] + d["same_region"] + 2 * d["same_pattern"]
            if d["same_device"] + d["same_card"] + d["same_region"] > 0:      # an entity link is required (as in LocalBackend)
                out.append(d)
        out.sort(key=lambda d: (d["score"], d["opened_at"]), reverse=True)   # best match first, newest first on ties
        return _clean(out[:k])

    # ---- autonomous monitoring ----------------------------------------------------------------------------------
    def find_rings(self, min_cards: int = 8, min_new: float = 0.9, min_proxy: float = 0.9) -> list[dict]:
        r = self._run("find_rings", min_cards=min_cards, min_new=min_new, min_proxy=min_proxy)
        return [{"device_profile": v["v_id"], **v["attributes"]} for v in (r.get("D") or [])]

    # ---- graph algorithms (#9): ring discovery beyond one device, hub scoring, card-to-card links -----------------
    # Devices are only ever hopped through when they look like a ring device, not merely a shared one: rare
    # (<= max_device_cards cards) AND mostly New AND mostly behind an anonymous proxy (find_rings' own signature).
    # A plain shared device (a household router, a popular browser) has low new_frac/proxy_frac and is never entered,
    # which is what keeps these traversals from flooding into the whole graph (an early cut at max_device_cards alone
    # still reaches thousands of cards in a few hops; the quality filters cut it to single digits of real devices).
    def ring_component(self, card_id: str, max_device_cards: int = 80, min_new_frac: float = 0.85,
                       min_proxy_frac: float = 0.85, max_hops: int = 6) -> dict:
        """The connected component of cards and devices reachable from `card_id` through ring-like devices. Real
        multi-hop connectivity (a graph traversal / connected-components query), not a same-device coincidence like
        `device_burst`: a ring built across two or three devices still shows up as one component."""
        r = self._run("ring_component", seed=(card_id, "Card"), max_device_cards=max_device_cards,
                      min_new_frac=min_new_frac, min_proxy_frac=min_proxy_frac, max_hops=max_hops)
        cards = [{"card_id": v["v_id"], "hop": v["attributes"]["hop"]} for v in (r.get("AllCards") or [])]
        devices = [{"device_profile": v["v_id"], "n_cards": v["attributes"]["n_cards"], "new_frac": v["attributes"]["new_frac"],
                   "proxy_frac": v["attributes"]["proxy_frac"], "hop": v["attributes"]["hop"]} for v in (r.get("AllDevices") or [])]
        cards.sort(key=lambda c: c["hop"])
        devices.sort(key=lambda d: d["hop"])
        return _clean({"seed": card_id, "cards": cards, "devices": devices, "n_cards": len(cards), "n_devices": len(devices)})

    def device_hub_rank(self, max_device_cards: int = 80, min_new_frac: float = 0.85, min_proxy_frac: float = 0.85,
                        iters: int = 4, top_k: int = 15) -> list[dict]:
        """Ring-like devices ranked by an iterative hub/authority score (HITS power iteration, renormalised each round,
        over the card-device bipartite graph): a device whose cards are themselves tied to many OTHER ring-like devices
        ranks above one with many cards but no further links, i.e. the centre of a coordinated ring rather than just a
        busy device. Scores are relative (max ~1.0 per round), for ranking, not a probability."""
        r = self._run("device_hub_rank", max_device_cards=max_device_cards, min_new_frac=min_new_frac,
                      min_proxy_frac=min_proxy_frac, iters=iters, top_k=top_k)
        out = [{"device_profile": v["v_id"], **v["attributes"]} for v in (r.get("TopDevices") or [])]
        out.sort(key=lambda d: -d["hub_score"])
        return out

    def card_link(self, card_a: str, card_b: str, max_device_cards: int = 80, min_new_frac: float = 0.85,
                 min_proxy_frac: float = 0.85, max_hops: int = 6) -> dict:
        """Are two cards linked through a chain of ring-like shared devices, and how far apart? Built on
        `ring_component`: b is linked to a iff it appears in a's component, at the reported hop distance."""
        comp = self.ring_component(card_a, max_device_cards=max_device_cards, min_new_frac=min_new_frac,
                                   min_proxy_frac=min_proxy_frac, max_hops=max_hops)
        hit = next((c for c in comp["cards"] if c["card_id"] == card_b), None)
        return {"card_a": card_a, "card_b": card_b, "linked": hit is not None,
                "hops": hit["hop"] if hit else None, "component_size": comp["n_cards"],
                "component_devices": [d["device_profile"] for d in comp["devices"]] if hit else []}


class TigerGraphCaseMemory:
    """Writes each investigation back as a FraudCase vertex linked to the card, transactions, devices and similar cases."""

    graph = True

    def __init__(self, conn=None):
        self.conn = conn or connect()

    def write(self, trig: dict, case: Case, inv, final_actions) -> tuple[str, bool]:
        gid = f"CASE-2016-{trig['case_id'].split('-')[-1]}"       # stable per benchmark case, so a rerun updates instead of duplicating
        c = self.conn
        c.upsertVertex("FraudCase", gid, {
            "case_id": trig["case_id"], "verdict": case.verdict, "pattern": case.pattern, "fraud_probability": case.fraud_probability,
            "exposure": case.exposure_usd, "status": case.status, "summary": case.summary[:2000],
            "created": time.strftime("%Y-%m-%d %H:%M:%S")})
        c.upsertEdge("Card", trig["card_id"], "HAS_CASE", "FraudCase", gid)
        for tid in case.affected_txn_ids:
            c.upsertEdge("FraudCase", gid, "CASE_TXN", "Transaction", tid)
        for dev in case.connected_device_profiles[:1]:
            c.upsertEdge("FraudCase", gid, "CASE_DEVICE", "DeviceProfile", dev)
        for cc in case.connected_card_ids:
            c.upsertEdge("FraudCase", gid, "CASE_CONNECTED", "Card", cc)
        for cid in case.similar_prior_cases:
            c.upsertEdge("FraudCase", gid, "SIMILAR_TO", "ClosedCase", cid)
        return gid, True

    def find(self, card_id: str | None = None, device_profile: str | None = None) -> list[dict]:
        out = []
        if card_id:
            for e in self.conn.getEdges("Card", card_id, "HAS_CASE") or []:
                v = self.conn.getVerticesById("FraudCase", e["to_id"])
                out += [x["attributes"] for x in (v if isinstance(v, list) else [v])]
        return out
