"""Investigate every case in case_pack.csv and write one answer file per case.

    python run_cases.py                # writes cases/HHG-001.json ... HHG-020.json
    python run_cases.py --case HHG-014 # one case, prints the full answer
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.backend import LocalBackend
from agent.memory import LocalCaseMemory
from agent.orchestrator import Orchestrator


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="cases")
    ap.add_argument("--case", default=None)
    ap.add_argument("--no-memory", action="store_true")
    a = ap.parse_args()

    backend = LocalBackend("final")
    orch = Orchestrator(backend, None if a.no_memory else LocalCaseMemory())
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    pack = backend.query("SELECT * FROM case_pack ORDER BY case_id").to_dict("records")
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
