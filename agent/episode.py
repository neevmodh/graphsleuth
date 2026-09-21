"""Second-stage episode model: is this transaction part of the same fraud episode as the alert?

For an alert (anchor transaction) it looks at every transaction on the card within +-72h and predicts membership in
the anchor's fraud episode from: the first-stage score, the transaction's own anomaly features, and its relation to the
anchor (time gap, same device / region / product / channel). The anchor's own membership probability is the case-level
fraud probability; the members above a threshold are the affected transactions (and so the exposure).

Trained on the bank's closed cases: real episodes as positives; cleared alerts, hard look-alikes and random activity
as negatives (whole window labelled 0, because the anchor is not in a fraud episode).
"""
from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from .backend import LocalBackend
from .scorer import ROOT

EP_PATH = ROOT / "data" / "store" / "episode.joblib"
EP_DEV_PATH = ROOT / "data" / "store" / "episode_dev.joblib"

FEATURES = [
    "p", "bank_risk", "amt", "amt_ratio_med", "dev_new", "dev_unseen", "addr1_new", "prod_new", "proxy", "online", "gap_prev_h", "n_1h",
    "dt_h", "abs_dt_h", "is_anchor", "same_channel", "same_prod", "same_device", "same_addr1", "amt_ratio_anchor",
    "a_p", "a_bank_risk", "a_online", "a_dev_new", "a_amt_ratio",
    "n_win", "max_p_win", "mean_p_win", "n_p03", "n_p05", "rank_p", "p_minus_max", "max_p_others_24h", "n_p03_24h",
]


def feats_for_window(win: pd.DataFrame, anchor_tid: int) -> pd.DataFrame:
    """Vectorised features for every candidate in one anchor's window (same code for training and inference)."""
    w = win.sort_values("ts").reset_index(drop=True).copy()
    for c in ("dev_new", "dev_unseen", "addr1_new", "prod_new", "proxy", "gap_prev_h", "n_1h", "amt_ratio_med", "bank_risk", "amt", "p_oof", "addr1"):
        w[c] = pd.to_numeric(w[c], errors="coerce").astype("float64")   # DuckDB nullable ints -> plain floats (NaN, not pd.NA)
    a = w[w["tid"] == anchor_tid]
    if a.empty:
        raise KeyError(anchor_tid)
    a = a.iloc[0]
    p = w["p_oof"].fillna(0.0)
    w["p"] = p
    w["online"] = (w["channel"] == "online").astype(float)
    dt = (w["ts"] - a["ts"]).dt.total_seconds() / 3600.0
    w["dt_h"], w["abs_dt_h"] = dt, dt.abs()
    w["is_anchor"] = (w["tid"] == anchor_tid).astype(float)
    w["same_channel"] = (w["channel"] == a["channel"]).astype(float)
    w["same_prod"] = (w["prod"] == a["prod"]).astype(float)
    w["same_device"] = ((w["device_profile"] == a["device_profile"]) & w["device_profile"].notna()).astype(float)
    w["same_addr1"] = ((w["addr1"] == a["addr1"]) & w["addr1"].notna()).astype(float)
    w["amt_ratio_anchor"] = w["amt"] / max(float(a["amt"]), 0.01)
    w["a_p"] = float(p[w["tid"] == anchor_tid].iloc[0])
    w["a_bank_risk"] = float(a["bank_risk"])
    w["a_online"] = float(a["channel"] == "online")
    w["a_dev_new"] = float(a["dev_new"]) if a["dev_new"] == a["dev_new"] else np.nan
    w["a_amt_ratio"] = float(a["amt_ratio_med"]) if a["amt_ratio_med"] == a["amt_ratio_med"] else np.nan
    w["n_win"] = float(len(w))
    w["max_p_win"], w["mean_p_win"] = float(p.max()), float(p.mean())
    w["n_p03"], w["n_p05"] = float((p >= 0.3).sum()), float((p >= 0.5).sum())
    w["rank_p"] = p.rank(ascending=False, method="min")
    w["p_minus_max"] = p - p.max()
    near = w["abs_dt_h"] <= 24
    others = near & (w["tid"] != anchor_tid)
    w["max_p_others_24h"] = float(p[others].max()) if others.any() else 0.0
    w["n_p03_24h"] = float(((p >= 0.3) & near).sum())
    return w[["tid", "ts"] + FEATURES].astype({c: "float32" for c in FEATURES})


RISK_BANDS = [(0.0, 0.2), (0.2, 0.5), (0.5, 0.7), (0.7, 1.01)]


def _anchors(b: LocalBackend, cutoff: str, seed: int = 7) -> pd.DataFrame:
    """Anchors + the transaction ids that count as their episode (empty set => negative anchor).

    Negatives are matched to positives WITHIN each bank-risk band, so at any risk score roughly half the anchors are
    legitimate. Without this the model learns 'medium risk score => fraud' (cleared alerts all score above 0.7), which
    the README warns is wrong in both directions. Cleared alerts are used first, then unflagged look-alikes."""
    fraud = b.query(f"""
        SELECT c.case_id, c.card_id, c.txn_ids FROM closed_cases c
        WHERE c.outcome = 'confirmed_fraud' AND c.txn_ids <> '' AND c.opened_at < '{cutoff}'""")
    rng = np.random.default_rng(seed)
    pos = []
    for r in fraud.itertuples():
        ids = [int(x) for x in r.txn_ids.split("|")]
        for a in set(rng.choice(ids, size=min(2, len(ids)), replace=False).tolist()):
            pos.append((int(a), r.card_id, set(ids)))
    risk = b.query(f"SELECT TransactionID tid, risk_score r FROM tx WHERE TransactionID IN ({','.join(str(p[0]) for p in pos)})").set_index("tid")["r"]
    rows = list(pos)
    cleared = b.query(f"""SELECT c.card_id, CAST(c.txn_ids AS BIGINT) tid, t.risk_score r FROM closed_cases c
                          JOIN tx t ON t.TransactionID = CAST(c.txn_ids AS BIGINT) WHERE c.outcome='cleared' AND c.opened_at < '{cutoff}'""")
    for r in cleared.itertuples():
        rows.append((int(r.tid), r.card_id, set()))
    lab = "SELECT DISTINCT CAST(unnest(string_split(txn_ids,'|')) AS BIGINT) tid FROM closed_cases WHERE txn_ids <> ''"
    for lo, hi in RISK_BANDS:
        n_pos = sum(1 for p in pos if lo <= risk[p[0]] < hi)
        n_clr = int(((cleared.r >= lo) & (cleared.r < hi)).sum())
        k = n_pos - n_clr
        if k <= 0:
            continue
        # filter first, sample after: DuckDB applies USING SAMPLE to the scan *before* WHERE unless it wraps a subquery
        df = b.query(f"""SELECT tid, card_id FROM (
              SELECT f.tid, f.card_id FROM feat f JOIN tx t ON t.TransactionID = f.tid
              WHERE f.ts < '{cutoff}' AND f.ts >= '2016-07-04' AND t.risk_score >= {lo} AND t.risk_score < {hi}
                AND f.tid NOT IN ({lab})) USING SAMPLE {k} ROWS""")
        for r in df.itertuples():
            rows.append((int(r.tid), r.card_id, set()))
    return pd.DataFrame(rows, columns=["tid", "card_id", "ids"])


def build_training(b: LocalBackend, cutoff: str, max_anchors: int | None = None) -> tuple[pd.DataFrame, np.ndarray]:
    an = _anchors(b, cutoff)
    if max_anchors:
        an = an.sample(min(max_anchors, len(an)), random_state=3)
    Xs, ys = [], []
    for r in an.itertuples():
        ts = b.query(f"SELECT ts FROM feat WHERE tid = {r.tid}").iloc[0, 0]
        w = b.card_window(r.card_id, str(ts), 72, 72)
        if w.empty:
            continue
        w = w.sort_values("ts")
        if len(w) > 250:   # keep the 250 transactions nearest the anchor
            w = w.iloc[(w["ts"] - ts).abs().argsort()[:250]]
        try:
            f = feats_for_window(w, r.tid)
        except KeyError:
            continue
        ys.append(f["tid"].isin(r.ids).astype(int).values)
        Xs.append(f)
    X = pd.concat(Xs, ignore_index=True)
    return X, np.concatenate(ys)


def train(cutoff: str, path: Path, max_anchors: int | None = None) -> dict:
    b = LocalBackend("final")
    X, y = build_training(b, cutoff, max_anchors)
    m = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.06, max_leaf_nodes=31, min_samples_leaf=30,
                                       l2_regularization=1.0, early_stopping=True, validation_fraction=0.1, random_state=5)
    m.fit(X[FEATURES], y)
    joblib.dump({"model": m, "features": FEATURES}, path)
    return {"rows": len(X), "pos": int(y.sum())}


class EpisodeModel:
    def __init__(self, path: Path = EP_PATH):
        b = joblib.load(path)
        self.model, self.features = b["model"], b["features"]

    def predict(self, win: pd.DataFrame, anchor_tid: int) -> pd.DataFrame:
        f = feats_for_window(win, anchor_tid)
        f["m"] = self.model.predict_proba(f[self.features])[:, 1]
        return f[["tid", "ts", "m", "p"]]


if __name__ == "__main__":
    import sys
    if "--final" in sys.argv:
        print(train("2016-11-01", EP_PATH))
    else:
        print(train("2016-10-01", EP_DEV_PATH))
