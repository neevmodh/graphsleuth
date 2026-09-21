"""Batch-score every transaction once and store the results as queryable attributes.

`scores_dev`   : models trained on data before 2016-10-01 (fair test on October closed cases)
`scores_final` : models trained on all labelled months (used for the exam period)
Columns: tid, p (calibrated fraud probability), p_raw, and one probability per pattern class.
In the TigerGraph deployment these become Transaction vertex attributes.
"""
from __future__ import annotations

import sys

import duckdb
import pandas as pd

from .pattern import CLASSES, PATTERN_DEV_PATH, PATTERN_PATH, PatternClassifier
from .scorer import DB, DEV_PATH, MODEL_PATH, Scorer

MONTHS = [("2016-07-01", "2016-08-01"), ("2016-08-01", "2016-09-01"), ("2016-09-01", "2016-10-01"),
          ("2016-10-01", "2016-11-01"), ("2016-11-01", "2016-12-01"), ("2016-12-01", "2017-01-01")]


def build(variant: str) -> int:
    scorer_path, pat_path = (DEV_PATH, PATTERN_DEV_PATH) if variant == "dev" else (MODEL_PATH, PATTERN_PATH)
    con = duckdb.connect(str(DB))
    con.execute("PRAGMA memory_limit='6GB'")
    sc, pc = Scorer(scorer_path), PatternClassifier(pat_path)
    parts = []
    for lo, hi in MONTHS:
        where = f"t.ts >= '{lo}' AND t.ts < '{hi}'"
        s = sc.score(con, where)
        p = pc.predict_where(con, where)
        parts.append(s.merge(p, on="tid", how="left"))
        print(variant, lo, len(s), flush=True)
    out = pd.concat(parts, ignore_index=True)
    out = out.rename(columns={c: f"pat_{c}" for c in CLASSES})
    con.execute(f"DROP TABLE IF EXISTS scores_{variant}")
    con.register("out_df", out)
    con.execute(f"CREATE TABLE scores_{variant} AS SELECT * FROM out_df")
    con.execute(f"CREATE INDEX idx_scores_{variant} ON scores_{variant}(tid)")
    return len(out)


if __name__ == "__main__":
    for v in sys.argv[1:] or ["dev", "final"]:
        print(v, build(v))
