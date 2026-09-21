"""Investigate every case in case_pack.csv and write one answer file per case.

    python run_cases.py                # writes cases/HHG-001.json ... HHG-020.json
    python run_cases.py --case HHG-014 # one case, prints the full answer
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import os

import pandas as pd

from agent.backend_factory import make_backend
from agent.orchestrator import Orchestrator


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="cases")
    ap.add_argument("--case", default=None)
    ap.add_argument("--no-memory", action="store_true")
    a = ap.parse_args()

    backend, memory, name = make_backend()           # GRAPHSLEUTH_BACKEND=tigergraph switches to the TigerGraph backend
    print(f"backend: {name}")
    orch = Orchestrator(backend, None if a.no_memory else memory)
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    data_dir = os.getenv("DATA_DIR", "../dataset/HHGOA_IEEE")
    pack = pd.read_csv(f"{data_dir}/case_pack.csv").sort_values("case_id").to_dict("records")
    for t in pack:
        if a.case and t["case_id"] != a.case:
            continue
        ans = orch.run_case(t)
        (out / f"{t['case_id']}.json").write_text(json.dumps(ans.model_dump(), indent=2) + "\n")
        c = ans.case
        fin = ",".join(x.action for x in ans.next_best_actions.final)
        print(f"{t['case_id']} {c.verdict[:5]:5} p={c.fraud_probability:.2f} {c.pattern[:16]:16} ${c.exposure_usd:>8.2f} "
              f"sar={str(ans.sar.file):5} {c.status:17} -> {fin}")
        if a.case:
            print(json.dumps(ans.model_dump(), indent=2))


if __name__ == "__main__":
    main()
