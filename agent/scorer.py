"""Calibrated transaction-level fraud scorer trained on the bank's *closed cases*.

Labels: transactions listed in `confirmed_fraud` closed cases are positive; everything else
before the exam period (cleared alerts and unflagged activity) is negative. All fraud in
July-October is in the closed-case file, so unlabelled transactions are clean negatives.

The scorer is ONE evidence input for the agent. It never decides an action: the policy engine does.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "store" / "graphsleuth.duckdb"
MODEL_PATH = ROOT / "data" / "store" / "scorer.joblib"

CAT_COLS = ["ProductCD", "card4", "card6", "P_emaildomain", "R_emaildomain", "DeviceType",
            "id_15", "id_23", "id_30", "id_31", "id_33", "id_34", "channel", "dow_s"]
FEAT_COLS = [
    "online", "dev_new", "proxy", "prior_n", "card_age_d", "gap_prev_h", "gap_next_h", "n_1h", "n_24h", "n_48h",
    "n_small_1h", "n_online_48h", "n_inperson_48h", "addr1_prior_n", "addr1_known_prior", "addr1_new",
    "prod_prior_n", "prod_new", "dev_prior_n", "dev_unseen", "pem_prior_n", "chan_new", "amt_ratio_med",
    "amt_z", "online_prior_frac", "dev_log_cards", "dev_txns", "dev_cards", "dev_new_frac", "dev_proxy_frac",
    "reg_cards", "reg_txns", "hod", "dow",
]
NUM_RAW = ["TransactionAmt", "card1", "card2", "card3", "card5", "addr1", "addr2", "dist1", "dist2"] \
    + [f"C{i}" for i in range(1, 15)] + [f"D{i}" for i in range(1, 16)] + [f"V{i}" for i in range(1, 340)] \
    + [f"id_{i:02d}" for i in range(1, 12)]
M_COLS = [f"M{i}" for i in range(1, 10)]


def load_matrix(con: duckdb.DuckDBPyConnection, where: str = "TRUE", use_risk: bool = True) -> pd.DataFrame:
    """One row per transaction: raw model features + behavioural features."""
    have = {r[0] for r in con.execute("DESCRIBE tx").fetchall()}
    raw = [c for c in NUM_RAW if c in have]
    cats = [c for c in CAT_COLS if c in have]
    m = [c for c in M_COLS if c in have]
    sel = ["t.TransactionID AS tid", "t.ts", "t.card_id"] + [f"t.{c}" for c in raw + cats + m] \
        + [f"f.{c}" for c in FEAT_COLS] + (["t.risk_score"] if use_risk else [])
    sel = [s for s in sel if not s.endswith(".dow_s")]
    df = con.execute(f"SELECT {', '.join(sel)} FROM tx t JOIN feat f ON f.tid = t.TransactionID WHERE {where}").df()
    m_map = {"T": 1.0, "F": 0.0, "M0": 0.0, "M1": 1.0, "M2": 2.0, "True": 1.0, "False": 0.0}
    for c in m:   # M1-3,5-9 are T/F (DuckDB may parse them as booleans); M4 is M0/M1/M2
        df[c] = df[c].astype("string").map(m_map).astype("float32")
    for c in cats:
        if c != "dow_s":
            df[c] = df[c].astype("category")
    for c in df.columns:
        if str(df[c].dtype) == "float64":
            df[c] = df[c].astype("float32")
    return df


def _xy(df: pd.DataFrame, fraud_ids: set[int]):
    X = df.drop(columns=["tid", "ts", "card_id"])
    y = df["tid"].isin(fraud_ids).astype(int).values
    return X, y


def fraud_txn_ids(con) -> set[int]:
    rows = con.execute(
        "SELECT unnest(string_split(txn_ids,'|')) FROM closed_cases WHERE outcome='confirmed_fraud' AND txn_ids<>''"
    ).fetchall()
    return {int(r[0]) for r in rows}


def _model(cat_mask):
    return HistGradientBoostingClassifier(
        max_iter=400, learning_rate=0.07, max_leaf_nodes=63, min_samples_leaf=40, l2_regularization=1.0,
        categorical_features=cat_mask, early_stopping=True, validation_fraction=0.1, n_iter_no_change=25,
        random_state=7)


def train(use_risk: bool = True, holdout_from: str | None = "2016-10-01", verbose: bool = True) -> dict:
    con = duckdb.connect(str(DB), read_only=True)
    fraud = fraud_txn_ids(con)
    df = load_matrix(con, "t.ts < '2016-11-01'", use_risk=use_risk)
    X, y = _xy(df, fraud)
    cat_mask = [str(X[c].dtype) == "category" for c in X.columns]
    out = {"use_risk": use_risk, "n_features": X.shape[1]}
    if holdout_from:
        tr = (df["ts"] < holdout_from).values
        m = _model(cat_mask).fit(X[tr], y[tr])
        p = m.predict_proba(X[~tr])[:, 1]
        yv = y[~tr]
        iso = IsotonicRegression(out_of_bounds="clip").fit(p, yv)
        out.update(auc=roc_auc_score(yv, p), ap=average_precision_score(yv, p),
                   brier_raw=brier_score_loss(yv, p), brier_cal=brier_score_loss(yv, iso.predict(p)),
                   n_val=int((~tr).sum()), pos_val=int(yv.sum()))
        # exam-like slice: fraud vs cleared-alert transactions (the hard, realistic negatives)
        cleared = {int(r[0]) for r in con.execute(
            "SELECT unnest(string_split(txn_ids,'|')) FROM closed_cases WHERE outcome='cleared'").fetchall()}
        vt = df.loc[~tr, "tid"].values
        mask = np.isin(vt, list(cleared)) | (yv == 1)
        if mask.sum() and len(set(yv[mask])) == 2:
            out["auc_fraud_vs_cleared"] = roc_auc_score(yv[mask], p[mask])
    if verbose:
        print(json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}))
    return out


DEV_PATH = ROOT / "data" / "store" / "scorer_dev.joblib"


def fit_and_save(train_end: str, calib_start: str, path: Path) -> Path:
    """Fit on labelled data before `train_end`; isotonic-calibrate on [calib_start, train_end)
    using a model that never saw that slice (time-ordered, no leakage)."""
    con = duckdb.connect(str(DB), read_only=True)
    fraud = fraud_txn_ids(con)
    df = load_matrix(con, f"t.ts < '{train_end}'")
    X, y = _xy(df, fraud)
    cat_mask = [str(X[c].dtype) == "category" for c in X.columns]
    tr = (df["ts"] < calib_start).values
    m0 = _model(cat_mask).fit(X[tr], y[tr])
    iso = IsotonicRegression(out_of_bounds="clip").fit(m0.predict_proba(X[~tr])[:, 1], y[~tr])
    m = _model(cat_mask).fit(X, y)
    joblib.dump({"model": m, "iso": iso, "columns": list(X.columns),
                 "categories": {c: list(X[c].cat.categories) for c in X.columns if str(X[c].dtype) == "category"}},
                path)
    return path


def fit_final() -> Path:      # exam model: all labelled months (Jul-Oct)
    return fit_and_save("2016-11-01", "2016-10-01", MODEL_PATH)


def fit_dev() -> Path:        # dev model: never sees October, so October closed cases are a fair test
    return fit_and_save("2016-10-01", "2016-09-01", DEV_PATH)


class Scorer:
    """Calibrated per-transaction fraud probability. One evidence input, never a verdict."""

    def __init__(self, path: Path = MODEL_PATH):
        b = joblib.load(path)
        self.model, self.iso, self.columns, self.categories = b["model"], b["iso"], b["columns"], b["categories"]

    def score(self, con: duckdb.DuckDBPyConnection, where: str) -> pd.DataFrame:
        """Return tid, p (calibrated), p_raw for transactions matching `where` (SQL over alias t)."""
        df = load_matrix(con, where)
        if df.empty:
            return pd.DataFrame({"tid": [], "p": [], "p_raw": []})
        X = df.drop(columns=["tid", "ts", "card_id"])
        for c, cats in self.categories.items():
            X[c] = pd.Categorical(X[c].astype("object"), categories=cats)
        X = X[self.columns]
        raw = self.model.predict_proba(X)[:, 1]
        return pd.DataFrame({"tid": df["tid"].values, "p_raw": raw, "p": self.iso.predict(raw)})


if __name__ == "__main__":
    import sys
    if "--final" in sys.argv:
        print(fit_final())
    elif "--dev" in sys.argv:
        print(fit_dev())
    else:
        train(use_risk="--no-risk" not in sys.argv)
