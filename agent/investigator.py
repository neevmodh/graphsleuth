"""Evidence gathering and assessment.

`Investigator.investigate` runs a budgeted tool loop against a GraphBackend, records every call as a Step
(streamed to the UI), and returns an `Investigation` holding the verdict inputs. It never chooses actions:
`agent/policy.py` does that from the `Assessment` this module builds.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from .backend import GraphBackend
from .policy import Assessment
from .schemas import Evidence

PATTERN_COLS = {
    "account_takeover": "pat_account_takeover", "card_not_present_fraud": "pat_card_not_present_fraud",
    "card_not_present_new_device": "pat_card_not_present_new_device", "out_of_region_use": "pat_out_of_region_use",
}
THRESHOLDS = (500.0, 1000.0, 2500.0, 5000.0, 10000.0)


@dataclass
class Cfg:
    win_before_h: float = 72.0
    win_after_h: float = 72.0
    episode_p: float = 0.35         # stage-1 fallback: a window txn joins the episode above this probability
    episode_m: float = 0.40         # stage-2: a window txn joins the episode above this membership probability
    episode_gap_h: float = 48.0     # ...if chained to the flagged txn with gaps no longer than this
    tiny_amt: float = 5.0
    report_logit: float = 2.5       # a customer's own report is strong evidence (likelihood ratio ~12): people rarely dispute legitimate charges
    fraud_p: float = 0.60           # verdict thresholds (uncertain in between)
    legit_p: float = 0.30
    ring_min_cards: int = 8
    ring_min_new_frac: float = 0.9
    ring_min_proxy_frac: float = 0.9
    struct_min_txns: int = 3


@dataclass
class Step:
    n: int
    tool: str
    args: dict
    summary: str
    ms: float


@dataclass
class Investigation:
    trigger: dict
    steps: list[Step] = field(default_factory=list)
    flagged: dict = field(default_factory=dict)
    profile: dict = field(default_factory=dict)
    window: pd.DataFrame | None = None
    p_flagged: float = 0.0
    p_case: float = 0.0
    verdict: str = "uncertain"
    pattern: str = "none"
    pattern_description: str = ""
    episode: list[int] = field(default_factory=list)
    exposure: float = 0.0
    first_suspicious: str = ""
    connected_cards: list[str] = field(default_factory=list)
    connected_devices: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    similar: list[dict] = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    assessment: Assessment | None = None
    ring_cards_detail: list[dict] = field(default_factory=list)
    span: tuple[str, str] | None = None       # first/last timestamp of the episode
    episode_scores: object = None             # per-window-txn membership probabilities (stage 2)


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _ids(tids) -> list[str]:
    return [str(int(t)) for t in tids]


class Investigator:
    def __init__(self, backend: GraphBackend, cfg: Cfg | None = None, episode_model=None):
        self.b = backend
        self.cfg = cfg or Cfg()
        self.em = episode_model          # second-stage episode model (agent/episode.py); None => stage-1 fallback

    # ---- tool wrapper ---------------------------------------------------------------------------
    def _call(self, inv: Investigation, tool: str, fn: Callable, args: dict, summarize: Callable, on_step=None):
        t0 = time.perf_counter()
        out = fn(**args)
        step = Step(len(inv.steps) + 1, tool, args, summarize(out), (time.perf_counter() - t0) * 1000)
        inv.steps.append(step)
        if on_step:
            on_step(step)
        return out

    # ---- main loop -------------------------------------------------------------------------------
    def investigate(self, trig: dict, on_step: Callable[[Step], None] | None = None) -> Investigation:
        cfg, b = self.cfg, self.b
        inv = Investigation(trigger=trig)
        tid, card = int(trig["flagged_txn_id"]), trig["card_id"]

        f = self._call(inv, "get_transaction", b.transaction, {"tid": tid},
                       lambda r: f"{r['channel']} ${r['amt']:.2f} {r['prod']} at {r['ts']}, bank risk {r.get('bank_risk'):.2f}, model p={(r.get('p') or 0):.2f}", on_step)
        inv.flagged = f
        ts = f["ts"]
        inv.p_flagged = float(f.get("p") or 0.0)

        prof = self._call(inv, "card_profile", b.card_profile, {"card_id": card, "before_ts": ts},
                          lambda r: f"{r['n_prior']} prior txns, median ${r['median_amt']}, online {round(r['online_frac'] or 0, 2)}, home region {r['home_region']}", on_step)
        inv.profile = prof

        win = self._call(inv, "card_window", b.card_window,
                         {"card_id": card, "ts": ts, "hours_before": cfg.win_before_h, "hours_after": cfg.win_after_h},
                         lambda r: f"{len(r)} txns within +-{int(cfg.win_before_h)}h, {int((r['p'] >= cfg.episode_p).sum())} scored >= {cfg.episode_p}", on_step)
        inv.window = win

        sig: dict = {"p_stage1": inv.p_flagged}
        em = None
        if self.em is not None and len(win) and (win["tid"] == tid).any():
            em = self._call(inv, "episode_model", lambda: self.em.predict(win, tid), {},
                            lambda r: f"context model: anchor membership p={float(r.loc[r.tid == tid, 'm'].iloc[0]):.2f}, "
                                      f"{int((r['m'] >= cfg.episode_m).sum())} of {len(r)} window txns look like the same episode", on_step)
            inv.p_flagged = float(em.loc[em["tid"] == tid, "m"].iloc[0])
        inv.episode_scores = em
        # ---- pattern detectors (graph/sequence rules) -------------------------------------------------
        testing = self._detect_testing(win, tid)
        struct = self._detect_structuring(win, tid)
        if testing["hit"] or struct["hit"]:
            self._call(inv, "sequence_detectors", lambda: None, {},
                       lambda _: ("card-testing run" if testing["hit"] else "") + (" threshold-structuring burst" if struct["hit"] else ""), on_step)
        sig["testing"], sig["structuring"] = testing, struct

        ring = {"hit": False}
        dev = f.get("device_profile")
        if dev and f["channel"] == "online":
            nb = self._call(inv, "device_neighbors", b.device_neighbors, {"device_profile": dev, "ts": ts, "days": 14},
                            lambda r: f"device on {r['cards']} cards, {round((r['new_frac'] or 0) * 100)}% New, {round((r['proxy_frac'] or 0) * 100)}% proxied, {len(r['closed_cases'])} closed cases", on_step)
            sig["device"] = {k: nb[k] for k in ("txns", "cards", "new_frac", "proxy_frac", "first_ts", "last_ts")}
            sig["device_closed_cases"] = nb["closed_cases"]
            if (nb["cards"] >= cfg.ring_min_cards and (nb["new_frac"] or 0) >= cfg.ring_min_new_frac
                    and (nb["proxy_frac"] or 0) >= cfg.ring_min_proxy_frac):
                burst = self._call(inv, "device_burst", b.device_burst, {"device_profile": dev, "ts": ts},
                                   lambda r: f"burst {r['burst_start'][:10]}..{r['burst_end'][:10]}: {len(r['cards'])} cards, {r['txns']} txns", on_step)
                ring = {"hit": True, "burst": burst, "device": dev}
        sig["ring"] = ring

        region = None
        if f["channel"] == "in_person" and f.get("addr1") == f.get("addr1"):
            region = self._call(inv, "region_history", b.region_history, {"card_id": card, "addr1": f["addr1"], "ts": ts},
                                lambda r: f"region {r['addr1']}: {r['n_before']} prior txns; +-5d: {r['in_region_near']} in region on {r['days_in_region_near']} day(s), {r['elsewhere_near']} elsewhere", on_step)
            sig["region"] = region
            sig["region_cluster"] = self._call(inv, "region_cluster", b.region_cluster, {"addr1": f["addr1"], "ts": ts, "days": 7},
                                               lambda r: f"region {r['addr1']}: {r['cards']} cards, {r['likely_fraud_txns']} likely-fraud txns in +-7d", on_step)

        rec = self._call(inv, "recurring_check", b.recurring_check, {"card_id": card, "tid": tid},
                         lambda r: f"{r['n_prior']} earlier identical-amount txns, {r['monthly_gaps']} roughly-monthly gaps, {r['rate_per_30d']}/30d -> recurring={r['is_recurring']}", on_step)
        sig["recurring"] = rec

        # ---- probability -------------------------------------------------------------------------------
        p = inv.p_flagged
        sig["p_model"] = p
        if trig["trigger_type"] == "customer_report":
            p = _sigmoid(_logit(p) + cfg.report_logit)
        if testing["hit"] and testing["flagged_in_run"]:
            p = max(p, 0.88)
        if struct["hit"] and struct["flagged_in_run"]:
            p = max(p, 0.90)
        if ring["hit"]:
            p = max(p, 0.92)
        if rec["is_recurring"] and not (testing["hit"] or struct["hit"] or ring["hit"]):
            p *= 0.4
        p = min(max(p, 0.01), 0.99)
        inv.p_case = p
        inv.verdict = "fraud" if p >= cfg.fraud_p else "legitimate" if p <= cfg.legit_p else "uncertain"

        # ---- episode + pattern ---------------------------------------------------------------------------
        self._assemble_episode(inv, win, tid, testing, struct, ring, em)

        # ---- memory ---------------------------------------------------------------------------------------
        sim = self._call(inv, "similar_cases", b.similar_cases,
                         {"card_id": card, "device_profile": dev, "addr1": f.get("addr1"), "pattern": inv.pattern,
                          "before_ts": trig["opened_at"], "exclude_case": trig.get("case_id"), "k": 5},
                         lambda r: f"{len(r)} similar closed cases: " + ", ".join(x["case_id"] for x in r[:5]), on_step)
        inv.similar = sim
        inv.signals = sig
        self._build_evidence(inv)
        inv.assessment = self._assessment(inv)
        return inv

    # ---- detectors --------------------------------------------------------------------------------------------
    def _detect_testing(self, win: pd.DataFrame, tid: int) -> dict:
        """R5: >=3 small online authorizations within an hour, then a larger purchase."""
        cfg = self.cfg
        w = win[win["channel"] == "online"].sort_values("ts")
        tiny = w[w["amt"] < cfg.tiny_amt]
        best = None
        ts_list = list(tiny["ts"])
        for i in range(len(ts_list)):
            j = i
            while j + 1 < len(ts_list) and (ts_list[j + 1] - ts_list[i]).total_seconds() <= 3600:
                j += 1
            if j - i + 1 >= 3:
                run = tiny.iloc[i:j + 1]
                after = w[(w["ts"] > run["ts"].max()) & (w["ts"] <= run["ts"].max() + pd.Timedelta(hours=24)) & (w["amt"] >= max(10.0, 5 * run["amt"].max()))]
                if len(after):
                    best = (run, after)
                    break
        if not best:
            return {"hit": False, "flagged_in_run": False}
        run, after = best
        tids = list(run["tid"]) + list(after["tid"])
        return {"hit": True, "run_tids": tids, "flagged_in_run": tid in tids, "large_cleared": bool((after["amt"] > 100).any()),
                "n_small": len(run), "larger_amt": float(after["amt"].max())}

    def _detect_structuring(self, win: pd.DataFrame, tid: int) -> dict:
        """Undocumented pattern: >=3 online purchases within an hour, each just under a round reporting/authorization
        threshold (within 10% below it) and well above the card's normal amount."""
        cfg = self.cfg
        w = win[(win["channel"] == "online")].sort_values("ts")
        for T in THRESHOLDS:
            near = w[(w["amt"] >= 0.9 * T) & (w["amt"] < T) & (w["amt_ratio_med"].fillna(9) >= 3)]
            ts_list = list(near["ts"])
            for i in range(len(ts_list)):
                j = i
                while j + 1 < len(ts_list) and (ts_list[j + 1] - ts_list[i]).total_seconds() <= 3600:
                    j += 1
                if j - i + 1 >= cfg.struct_min_txns:
                    run = near.iloc[i:j + 1]
                    tids = list(run["tid"])
                    return {"hit": True, "threshold": T, "run_tids": tids, "flagged_in_run": tid in tids,
                            "total": float(run["amt"].sum()), "n": len(run)}
        return {"hit": False, "flagged_in_run": False}

    # ---- episode ------------------------------------------------------------------------------------------------
    def _assemble_episode(self, inv: Investigation, win: pd.DataFrame, tid: int, testing: dict, struct: dict, ring: dict, em=None) -> None:
        cfg = self.cfg
        ep: list[int] = []
        mine = None
        if ring["hit"]:
            card = inv.trigger["card_id"]
            mine = next((c for c in ring["burst"]["cards"] if c["card_id"] == card), None)
            ep = [int(x) for x in (mine["tids"] if mine else [tid])]
            inv.pattern = "undocumented"
            dev = ring["device"]
            n_cards = len(ring["burst"]["cards"])
            inv.pattern_description = (
                f"A single rare device profile ({dev}) transacts on {n_cards} different cards inside one burst "
                f"({ring['burst']['burst_start'][:10]} to {ring['burst']['burst_end'][:10]}). Every transaction shows the device as New "
                f"for the account and behind an anonymous proxy, purchases are ordinary online amounts, and the bank's risk scores stay low, "
                f"so no single transaction looks alarming. Found by expanding from the flagged transaction to its device and counting distinct cards.")
            inv.connected_cards = sorted(c["card_id"] for c in ring["burst"]["cards"] if c["card_id"] != card)
            inv.connected_devices = [dev]
            inv.ring_cards_detail = ring["burst"]["cards"]
        elif struct["hit"] and struct["flagged_in_run"]:
            ep = [int(x) for x in struct["run_tids"]]
            inv.pattern = "undocumented"
            inv.pattern_description = (
                f"{struct['n']} online purchases within an hour on one card, each just under ${struct['threshold']:,.0f} "
                f"(${win[win['tid'].isin(ep)]['amt'].min():,.2f} to ${win[win['tid'].isin(ep)]['amt'].max():,.2f}, about "
                f"{float(win[win['tid'].isin(ep)]['amt_ratio_med'].median()):.0f}x the card's normal amount). Amounts appear chosen to stay under an "
                f"authorization or reporting threshold while draining the card; risk scores stay low. Found by scanning the card's recent window for "
                f"bursts of near-threshold amounts.")
        elif testing["hit"] and testing["flagged_in_run"]:
            ep = [int(x) for x in testing["run_tids"]]
            inv.pattern = "card_testing"
        elif inv.verdict != "legitimate":
            ep = self._model_episode(win, tid, em)
            inv.pattern = self._classify_pattern(inv, win, ep)
        else:
            inv.pattern = "none"
        inv.episode = sorted(set(ep))
        if inv.verdict == "legitimate":
            inv.episode = []
        sub = win[win["tid"].isin(inv.episode)]
        inv.exposure = round(float(sub["amt"].abs().sum()), 2) if len(sub) else 0.0
        inv.first_suspicious = str(int(sub.sort_values("ts")["tid"].iloc[0])) if len(sub) else ""
        if len(sub):
            inv.span = (sub["ts"].min().strftime("%Y-%m-%d %H:%M:%S"), sub["ts"].max().strftime("%Y-%m-%d %H:%M:%S"))
        if mine:   # ring transactions can lie outside the +-72h window: use the burst record
            inv.exposure = round(float(mine["total"]), 2)
            inv.first_suspicious = str(int(mine["tids"][0]))
            inv.span = (mine["first_ts"], mine["last_ts"])

    def _model_episode(self, win: pd.DataFrame, tid: int, em=None) -> list[int]:
        cfg = self.cfg
        if em is not None:      # learned membership: all window txns judged to belong to the anchor's episode
            keep = set(int(x) for x in em.loc[em["m"] >= cfg.episode_m, "tid"]) | {tid}
            return sorted(keep)
        w = win.sort_values("ts").reset_index(drop=True)
        cand = (w["p"].fillna(0) >= cfg.episode_p) | (w["tid"] == tid)
        idx = w.index[w["tid"] == tid]
        if len(idx) == 0:
            return [tid]
        a = idx[0]
        keep = {a}
        gap = cfg.episode_gap_h * 3600
        i = a
        while i - 1 >= 0 and cand[i - 1] and (w.loc[i, "ts"] - w.loc[i - 1, "ts"]).total_seconds() <= gap:
            i -= 1; keep.add(i)
        i = a
        while i + 1 < len(w) and cand[i + 1] and (w.loc[i + 1, "ts"] - w.loc[i, "ts"]).total_seconds() <= gap:
            i += 1; keep.add(i)
        return [int(w.loc[k, "tid"]) for k in sorted(keep)]

    def _classify_pattern(self, inv: Investigation, win: pd.DataFrame, ep: list[int]) -> str:
        """Pattern from the episode's structure. The bank's labels follow exact rules for channel and device, so
        those are applied directly; only in-person-only episodes (account takeover vs out-of-region use) need the
        learned classifier."""
        sub = win[win["tid"].isin(ep)]
        if sub.empty or inv.p_case < 0.5:
            return "none"
        online = sub["channel"] == "online"
        if online.all():                                   # card-not-present family
            tiny = sub[sub["amt"] < self.cfg.tiny_amt]
            if len(tiny) >= 2 and sub["amt"].max() >= max(10.0, 5 * tiny["amt"].max()):
                return "card_testing"                      # small probes followed by a much larger purchase
            new_dev = pd.to_numeric(sub["dev_new"], errors="coerce").fillna(0).eq(1).any()
            return "card_not_present_new_device" if new_dev else "card_not_present_fraud"
        if online.any():                                   # mixed channel
            return "account_takeover"
        m = {k: float(sub[PATTERN_COLS[k]].fillna(0).mean()) for k in ("account_takeover", "out_of_region_use")}
        return max(m, key=m.get)

    # ---- evidence ---------------------------------------------------------------------------------------------------
    def _build_evidence(self, inv: Investigation) -> None:
        f, sig, prof, tid = inv.flagged, inv.signals, inv.profile, int(inv.trigger["flagged_txn_id"])
        ev: list[Evidence] = []
        add = lambda claim, source, ref, ids=(): ev.append(Evidence(claim=claim, source=source, ref=ref, entity_ids=list(ids)))
        p1 = sig.get("p_stage1", inv.p_flagged)
        add(f"The calibrated transaction-level fraud model scores the flagged transaction {p1:.2f}; the bank's own risk score is "
            f"{f.get('bank_risk'):.2f} and is treated as an input, not a verdict.", "graph", f"query:score(txn={tid})", [str(tid)])
        if abs(inv.p_flagged - p1) > 0.05:
            n_ep = len(inv.episode)
            add(f"Judged in context (neighbouring transactions on the card, timing, shared device, region and product) the alert has a fraud probability of "
                f"{inv.p_flagged:.2f}, and {n_ep} transaction(s) look like one episode.", "graph", f"query:episode_model(txn={tid})",
                _ids(inv.episode) or [str(tid)])
        if f.get("amt_ratio_med") is not None and f["amt_ratio_med"] == f["amt_ratio_med"] and f["amt_ratio_med"] >= 3:
            add(f"Amount ${f['amt']:.2f} is {f['amt_ratio_med']:.1f}x the card's prior median (${prof.get('median_amt')}).", "graph",
                f"query:card_profile(card={inv.trigger['card_id']})", [str(tid)])
        if f.get("dev_unseen") == 1:
            add("The device profile has never been seen on this card before.", "graph", f"query:card_window(card={inv.trigger['card_id']})", [str(tid)])
        if f.get("prod_new") == 1:
            add(f"Product code {f['prod']} has never been used on this card.", "graph", f"query:card_profile(card={inv.trigger['card_id']})", [str(tid)])
        if sig.get("testing", {}).get("hit"):
            t = sig["testing"]
            add(f"{t['n_small']} very small online authorizations within an hour followed by a purchase of ${t['larger_amt']:.2f}: card-testing sequence (R5).",
                "graph", f"query:testing_sequence(card={inv.trigger['card_id']})", _ids(t["run_tids"]))
        if sig.get("structuring", {}).get("hit"):
            s = sig["structuring"]
            add(f"{s['n']} online purchases within an hour, each just under ${s['threshold']:,.0f}, totalling ${s['total']:,.2f}: consistent with structuring under an authorization threshold.",
                "graph", f"query:card_window(card={inv.trigger['card_id']})", _ids(s["run_tids"]))
        if sig.get("ring", {}).get("hit"):
            b = sig["ring"]["burst"]
            add(f"Device profile '{sig['ring']['device']}' was used on {len(b['cards'])} cards in one burst ({b['burst_start'][:10]} to {b['burst_end'][:10]}), "
                f"always marked New and behind an anonymous proxy.", "graph", f"query:device_burst(device={sig['ring']['device']})",
                [c["card_id"] for c in b["cards"]][:25])
        elif sig.get("device"):
            d = sig["device"]
            add(f"Device profile is shared by {d['cards']} cards overall ({round((d['new_frac'] or 0) * 100)}% New, {round((d['proxy_frac'] or 0) * 100)}% proxied): "
                f"{'common consumer device, weak link' if d['cards'] > 50 else 'limited sharing'}.", "graph", "query:device_neighbors", [])
        if sig.get("region"):
            r = sig["region"]
            trip = r["days_in_region_near"] >= 2
            add(f"Billing region {r['addr1']}: {r['n_before']} prior transactions on this card; within +-5 days {r['in_region_near']} in-region transactions on "
                f"{r['days_in_region_near']} day(s) and {r['elsewhere_near']} elsewhere ({'consistent with travel' if trip and r['n_before'] == 0 else 'see pattern assessment'}).",
                "graph", f"query:region_history(card={inv.trigger['card_id']},addr1={r['addr1']})", [str(tid)])
        if sig.get("recurring", {}).get("is_recurring"):
            r = sig["recurring"]
            add(f"The identical amount and product occurred {r['n_prior']} times earlier on this card at roughly monthly gaps ({r['gaps_days']} days, {r['rate_per_30d']} per 30 days): matches the customer's own recurring pattern (R7). No merchant field exists, so amount, product and cadence are the proxy.",
                "graph", f"query:recurring_check(txn={tid})", [str(tid)])
        if inv.trigger["trigger_type"] == "customer_report":
            add("The cardholder reported this transaction as unauthorized.", "customer", f"trigger:{inv.trigger['case_id']}", [str(tid)])
        for c in inv.similar[:3]:
            add(f"Closed case {c['case_id']} ({c['outcome']}, {c['pattern']}, ${c['exposure']:.2f}) is relevant: "
                + ", ".join(k.replace('same_', 'same ') for k in ("same_device", "same_card", "same_region", "same_pattern") if c.get(k)) + ".",
                "graph", f"query:similar_cases", [c["case_id"]])
        inv.evidence = ev

    # ---- assessment for the policy engine ---------------------------------------------------------------------------------
    def _assessment(self, inv: Investigation) -> Assessment:
        sig, f = inv.signals, inv.flagged
        families = 0
        fraud_lean = inv.p_case >= 0.5
        if fraud_lean:
            families += int(inv.p_flagged >= 0.4)                                     # model
            families += int(bool(f.get("dev_unseen") == 1 or f.get("prod_new") == 1 or f.get("addr1_new") == 1
                                 or (f.get("amt_ratio_med") or 0) >= 3))               # behavioural anomaly
            families += int(sig["testing"]["hit"] or sig["structuring"]["hit"] or (f.get("n_1h") or 0) >= 3)   # sequence
            families += int(sig["ring"]["hit"])                                        # graph link
            families += int(any(c.get("outcome") == "confirmed_fraud" and (c.get("same_device") or c.get("same_card")) for c in inv.similar))   # memory
            families += int(inv.trigger["trigger_type"] == "customer_report")          # customer
        else:
            families += int(inv.p_flagged <= 0.15)
            families += int(f.get("dev_unseen") == 0 and f.get("prod_new") == 0)
            families += int(sig["recurring"]["is_recurring"])
            families += int(not sig["testing"]["hit"] and not sig["structuring"]["hit"] and not sig["ring"]["hit"])
        recurring = sig["recurring"]["is_recurring"] and inv.trigger["trigger_type"] == "customer_report"
        conflict = (inv.trigger["trigger_type"] == "customer_report" and inv.p_flagged < 0.15 and not recurring)
        return Assessment(
            fraud_probability=inv.p_case, exposure_usd=inv.exposure, trigger_type=inv.trigger["trigger_type"],
            n_independent_evidence=max(families, 1), evidence_conflict=conflict, pattern=inv.pattern,
            card_testing=bool(sig["testing"]["hit"] and sig["testing"]["flagged_in_run"]),
            large_purchase_cleared=bool(sig["testing"].get("large_cleared")),
            shared_origin=bool(sig["ring"]["hit"]), connected_fraud_cards=list(inv.connected_cards) if sig["ring"]["hit"] else [],
            recurring_dispute=bool(recurring), undocumented_coordinated=bool(sig["ring"]["hit"]))
