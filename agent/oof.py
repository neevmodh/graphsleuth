"""Out-of-fold first-stage scores for the labelled months.

The second-stage episode model is trained on first-stage scores. If those scores came from a model that had
seen the same transactions' labels they would be optimistic and the second stage would over-trust them. So each
labelled month is scored by a model trained on the *other* labelled months. Nov-Dec (the exam period) use the final
model. Stored as `scores_oof(tid, p)`; p is the raw (uncalibrated) probability, which is all a tree model needs.
"""
from __future__ import annotations

import duckdb
import pandas as pd

from .scorer import DB, MODEL_PATH, Scorer, _model, _xy, fraud_txn_ids, load_matrix

FOLDS = [("2016-07-01", "2016-08-01"), ("2016-08-01", "2016-09-01"), ("2016-09-01", "2016-10-01"), ("2016-10-01", "2016-11-01")]


def build() -> int:
    con = duckdb.connect(str(DB))
    con.execute("PRAGMA memory_limit='6GB'")
    fraud = fraud_txn_ids(con)
    df = load_matrix(con, "t.ts < '2016-11-01'")
    X, y = _xy(df, fraud)
    cat_mask = [str(X[c].dtype) == "category" for c in X.columns]
    parts = []
    for lo, hi in FOLDS:
        te = ((df["ts"] >= lo) & (df["ts"] < hi)).values
        m = _model(cat_mask).fit(X[~te], y[~te])
        parts.append(pd.DataFrame({"tid": df.loc[te, "tid"].values, "p": m.predict_proba(X[te])[:, 1]}))
        print("fold", lo, int(te.sum()), flush=True)
    sc = Scorer(MODEL_PATH)
    for lo, hi in [("2016-11-01", "2016-12-01"), ("2016-12-01", "2017-01-01")]:
        s = sc.score(con, f"t.ts >= '{lo}' AND t.ts < '{hi}'")
        parts.append(pd.DataFrame({"tid": s["tid"].values, "p": s["p_raw"].values}))
    out = pd.concat(parts, ignore_index=True)
    con.execute("DROP TABLE IF EXISTS scores_oof")
    con.register("oof_df", out)
    con.execute("CREATE TABLE scores_oof AS SELECT * FROM oof_df")
    con.execute("CREATE INDEX idx_scores_oof ON scores_oof(tid)")
    return len(out)


if __name__ == "__main__":
    print(build())
