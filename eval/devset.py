"""Dev-set replay: run held-out closed cases through the agent with the outcome hidden and score the result.

Uses the leak-free `dev` models (trained before 2016-10-01) and only October closed cases, so the agent never saw
these labels. The scoring mirrors the dimensions of the answer format: verdict, calibration, pattern, affected
transactions, exposure, report decision and action set.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from agent.backend import LocalBackend  # noqa: E402
from agent.investigator import Cfg  # noqa: E402
from agent.orchestrator import UNCERTAIN_HI, UNCERTAIN_LO, Orchestrator  # noqa: E402


def sample_cases(b: LocalBackend, per_group: int, seed: int, start: str = "2016-10-01") -> list[dict]:
    rng = random.Random(seed)
    cc = b.query(f"SELECT * FROM closed_cases WHERE opened_at >= '{start}' AND txn_ids <> ''")
    out = []
    for grp, g in cc.groupby(cc["outcome"].where(cc["outcome"] == "cleared", cc["pattern"])):
        rows = g.to_dict("records")
        rng.shuffle(rows)
        out += rows[:per_group]
    # legitimate look-alikes: unflagged October transactions with an elevated bank risk score (never in any case)
    la = b.query(f"""SELECT * FROM (SELECT f.tid, f.card_id, f.customer_id, f.ts FROM feat f JOIN tx t ON t.TransactionID = f.tid
        WHERE f.ts >= '{start}' AND f.ts < '2016-11-01' AND t.risk_score >= 0.5 AND f.tid NOT IN
          (SELECT CAST(unnest(string_split(txn_ids,'|')) AS BIGINT) FROM closed_cases WHERE txn_ids <> '')) USING SAMPLE {per_group} ROWS""")
    for r in la.itertuples():
        out.append({"case_id": f"LA-{r.tid}", "customer_id": r.customer_id, "card_id": r.card_id,
                    "opened_at": r.ts + pd.Timedelta(hours=6), "outcome": "cleared", "pattern": "none", "txn_ids": str(r.tid),
                    "actions_taken": "VERIFY_WITH_CUSTOMER|CLOSE_NO_FRAUD", "report_filed": False, "exposure_usd": 0.0, "src": "legit_alert"})
    return out


def make_trigger(b: LocalBackend, case: dict, rng: random.Random) -> dict:
    tids = [int(x) for x in case["txn_ids"].split("|")]
    if case["outcome"] == "cleared" or rng.random() < 0.5:
        ttype = "risk_score"
        r = b.query(f"SELECT TransactionID, risk_score FROM tx WHERE TransactionID IN ({','.join(map(str, tids))}) ORDER BY risk_score DESC LIMIT 1")
        tid, rs = int(r.iloc[0, 0]), float(r.iloc[0, 1])
    else:
        ttype, tid, rs = "customer_report", rng.choice(tids), None
    return {"case_id": case["case_id"], "opened_at": str(case["opened_at"]), "trigger_type": ttype,
            "trigger_text": "dev replay", "flagged_txn_id": tid, "card_id": case["card_id"],
            "customer_id": case["customer_id"], "risk_score": rs}


def run(n: int, seed: int, cfg: Cfg | None, verbose: bool = False) -> dict:
    b = LocalBackend("dev")
    orch = Orchestrator(b, None, cfg or Cfg(fraud_p=UNCERTAIN_HI, legit_p=UNCERTAIN_LO))
    rng = random.Random(seed)
    cases = sample_cases(b, n, seed)
    rows = []
    for c in cases:
        trig = make_trigger(b, c, rng)
        ans = orch.run_case(trig, write_memory=False)
        truth_fraud = c["outcome"] == "confirmed_fraud"
        true_tx = set(c["txn_ids"].split("|")) if truth_fraud else set()
        pred_tx = set(ans.case.affected_txn_ids)
        tp = len(true_tx & pred_tx)
        prec = tp / len(pred_tx) if pred_tx else (1.0 if not true_tx else 0.0)
        rec = tp / len(true_tx) if true_tx else 1.0
        f1 = 0.0 if prec + rec == 0 else 2 * prec * rec / (prec + rec)
        true_actions = set(c["actions_taken"].split("|")) | ({"FILE_REPORT"} if c["report_filed"] else set())
        pred_actions = {a.action for a in ans.next_best_actions.final}
        j = len(true_actions & pred_actions) / len(true_actions | pred_actions)
        rows.append(dict(case_id=c["case_id"], src=c.get("src", "closed"), trig=trig["trigger_type"], truth=int(truth_fraud), true_pattern=c["pattern"],
                         p=ans.case.fraud_probability, verdict=ans.case.verdict, pred_pattern=ans.case.pattern,
                         f1=f1, prec=prec, rec=rec, true_exp=float(c["exposure_usd"]) if truth_fraud else 0.0,
                         pred_exp=ans.case.exposure_usd, sar_true=bool(c["report_filed"]), sar_pred=ans.sar.file,
                         jaccard=j, n_true=len(true_tx), n_pred=len(pred_tx), steps=ans.tool_calls))
    return summarize(pd.DataFrame(rows), verbose)


def summarize(df: pd.DataFrame, verbose: bool) -> dict:
    fr = df[df.truth == 1]
    m = {"n": len(df), "n_fraud": len(fr), "n_cleared": int(((df.truth == 0) & (df.src == "closed")).sum()), "n_legit_alert": int((df.src == "legit_alert").sum())}
    m["auc"] = roc_auc_score(df.truth, df.p) if df.truth.nunique() > 1 else float("nan")
    m["brier"] = brier_score_loss(df.truth, df.p)
    dec = df[df.verdict != "uncertain"]
    m["uncertain_rate"] = float((df.verdict == "uncertain").mean())
    m["verdict_acc_decided"] = float(((dec.verdict == "fraud") == (dec.truth == 1)).mean()) if len(dec) else float("nan")
    m["verdict_acc_p50"] = float(((df.p >= 0.5) == (df.truth == 1)).mean())
    m["fraud_recall_p50"] = float((fr.p >= 0.5).mean())
    m["cleared_fpr_p50"] = float((df[(df.truth == 0) & (df.src == "closed")].p >= 0.5).mean())
    la = df[df.src == "legit_alert"]
    m["legit_alert_fpr_p50"] = float((la.p >= 0.5).mean()) if len(la) else float("nan")
    m["legit_alert_mean_p"] = float(la.p.mean()) if len(la) else float("nan")
    pf = fr[fr.verdict != "legitimate"]
    m["pattern_acc"] = float((pf.pred_pattern == pf.true_pattern).mean()) if len(pf) else float("nan")
    m["episode_f1"] = float(fr.f1.mean())
    m["episode_prec"], m["episode_rec"] = float(fr.prec.mean()), float(fr.rec.mean())
    m["exposure_abs_err_median"] = float((fr.pred_exp - fr.true_exp).abs().median())
    m["exposure_rel_err_median"] = float(((fr.pred_exp - fr.true_exp).abs() / fr.true_exp.clip(lower=1)).median())
    m["sar_acc"] = float((df.sar_pred == df.sar_true).mean())
    m["action_jaccard"] = float(df.jaccard.mean())
    m["steps_mean"] = float(df.steps.mean())
    if verbose:
        print(df.groupby("true_pattern").agg(n=("p", "size"), p=("p", "mean"), f1=("f1", "mean"), jac=("jaccard", "mean"),
                                             fraud_verdict=("verdict", lambda s: (s == "fraud").mean()),
                                             sar_ok=("sar_pred", "mean")).round(3).to_string())
        pf2 = fr[fr.verdict != "legitimate"]
        if len(pf2):
            print(pd.crosstab(pf2.true_pattern, pf2.pred_pattern).to_string())
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60, help="cases per group")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--episode-p", type=float, default=None)
    ap.add_argument("--gap-h", type=float, default=None)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    cfg = Cfg(fraud_p=UNCERTAIN_HI, legit_p=UNCERTAIN_LO)
    if a.episode_p is not None:
        cfg.episode_p = a.episode_p
    if a.gap_h is not None:
        cfg.episode_gap_h = a.gap_h
    print(json.dumps(run(a.n, a.seed, cfg, a.v), indent=1))
