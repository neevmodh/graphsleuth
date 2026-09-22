"""Pattern classifier: which kind of fraud (or false alarm) does this transaction look like?

Trained on the closed cases' labelled transactions. Classes: the documented patterns present in the
history (card_not_present_fraud, card_not_present_new_device, out_of_region_use, account_takeover) and
`none` (cleared false alarms). Card testing and the undocumented device ring are too rare to learn
(16 and 9 cases), so they are detected by explicit sequence/graph rules in the investigator.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import classification_report, confusion_matrix

from .scorer import DB, FEAT_COLS, ROOT

PATTERN_PATH = ROOT / "data" / "store" / "pattern.joblib"
PATTERN_DEV_PATH = ROOT / "data" / "store" / "pattern_dev.joblib"
CLASSES = ["account_takeover", "card_not_present_fraud", "card_not_present_new_device", "out_of_region_use", "none"]
EXTRA = ["TransactionAmt", "risk_score", "dist1", "addr2"]
CAT = ["ProductCD", "card6", "id_15", "id_23"]


def _load(con, where: str) -> pd.DataFrame:
    cols = ", ".join([f"f.{c}" for c in FEAT_COLS] + [f"t.{c}" for c in EXTRA + CAT])
    df = con.execute(f"SELECT t.TransactionID AS tid, t.ts, {cols} FROM tx t JOIN feat f ON f.tid=t.TransactionID WHERE {where}").df()
    return df


def _prep(df: pd.DataFrame, cats: dict | None = None):
    X = df.drop(columns=[c for c in ("tid", "ts", "pattern") if c in df.columns]).copy()   # never feed the label
    cats = cats or {c: sorted(X[c].dropna().astype(str).unique().tolist()) for c in CAT}
    for c in CAT:
        X[c] = pd.Categorical(X[c].astype("object").astype("string").astype("object"), categories=cats[c])
    for c in X.columns:
        if c not in CAT:
            X[c] = pd.to_numeric(X[c], errors="coerce").astype("float32")
    return X, cats


def _labels(con) -> pd.DataFrame:
    return con.execute("""
        SELECT cast(unnest(string_split(txn_ids,'|')) AS BIGINT) AS tid, pattern FROM closed_cases
        WHERE txn_ids<>'' AND pattern IN ('account_takeover','card_not_present_fraud','card_not_present_new_device','out_of_region_use','none')
    """).df()


def train(train_end: str, path: Path, holdout_from: str | None = None) -> dict:
    con = duckdb.connect(str(DB), read_only=True)
    lab = _labels(con)
    df = _load(con, f"t.ts < '{train_end}'").merge(lab, on="tid", how="inner")
    X, cats = _prep(df)
    y = df["pattern"].map({c: i for i, c in enumerate(CLASSES)}).values
    out = {}
    cat_mask = [c in CAT for c in X.columns]
    mk = lambda: HistGradientBoostingClassifier(max_iter=250, learning_rate=0.08, max_leaf_nodes=31, min_samples_leaf=20,
                                                categorical_features=cat_mask, random_state=7)
    if holdout_from:
        tr = (df["ts"] < holdout_from).values
        m = mk().fit(X[tr], y[tr])
        pred = m.predict(X[~tr])
        out["acc"] = float((pred == y[~tr]).mean())
        out["report"] = classification_report(y[~tr], pred, target_names=CLASSES, digits=3, zero_division=0)
        out["confusion"] = confusion_matrix(y[~tr], pred).tolist()
    m = mk().fit(X, y)
    joblib.dump({"model": m, "cats": cats, "columns": list(X.columns), "classes": CLASSES}, path)
    return out


class PatternClassifier:
    def __init__(self, path: Path = PATTERN_PATH):
        b = joblib.load(path)
        self.model, self.cats, self.columns, self.classes = b["model"], b["cats"], b["columns"], b["classes"]

    def predict_where(self, con: duckdb.DuckDBPyConnection, where: str) -> pd.DataFrame:
        df = _load(con, where)
        if df.empty:
            return pd.DataFrame(columns=["tid", *self.classes])
        X, _ = _prep(df, self.cats)
        P = self.model.predict_proba(X[self.columns])
        out = pd.DataFrame(P, columns=self.classes)
        out.insert(0, "tid", df["tid"].values)
        return out


if __name__ == "__main__":
    import sys
    if "--final" in sys.argv:
        train("2016-11-01", PATTERN_PATH)
        print("saved", PATTERN_PATH)
    else:
        r = train("2016-10-01", PATTERN_DEV_PATH, holdout_from="2016-09-01")
        print("holdout acc", round(r["acc"], 4)); print(r["report"]); print(np.array(r["confusion"]))
