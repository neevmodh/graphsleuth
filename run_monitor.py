"""Autonomous monitor (issue #26, innovation only): sweeps the whole graph for alerts the 20 sampled benchmark cases
never triggered on, and investigates them exactly the way run_cases.py does. Two sweeps:

  ring sweep   -- every ring-like device find_rings() surfaces graph-wide (not just HHG-014's), one alert on its
                  highest-value card that isn't already one of the 20 cases.
  risk triage  -- the highest bank-risk-score transactions in the whole dataset that never became one of the 20
                  cases, i.e. what an always-on monitor would have escalated that the sampled alerts missed.

Output goes to cases_extra/ as MON-101, MON-102, ... (never MON-0xx, so TigerGraphCaseMemory's id scheme can never
collide with a real HHG case number) and is never written back to the graph (write_memory=False): this is exploratory,
not part of the 20 graded answers.

    python run_monitor.py                 # writes cases_extra/MON-*.json
    python run_monitor.py --rings 5 --risk 5
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import duckdb
import pandas as pd

from agent.backend_factory import make_backend, make_rag
from agent.llm import LLMRouter
from agent.orchestrator import Orchestrator
from agent.scorer import DB


def ring_alerts(backend, known_cards: set[str], limit: int) -> list[dict]:
    """One alert per ring-like device found anywhere in the graph, on its highest-total card not already
    investigated. Uses the same find_rings() signature as the R6 ring rule, so this is the same definition of
    'ring' the policy engine already knows how to act on, just swept over every device instead of one alert."""
    out = []
    for d in backend.find_rings(min_cards=8, min_new=0.85, min_proxy=0.85):
        nb = backend.device_neighbors(d["device_profile"], "2016-12-31 23:59:59", days=3650)
        cands = [c for c in nb["window_cards"] if c["card_id"] not in known_cards]
        if not cands:
            continue
        top = max(cands, key=lambda c: c["total"])
        out.append({"card_id": top["card_id"], "flagged_ts": top["last_ts"], "reason": f"ring device ({d['n_cards']} cards)"})
        if len(out) >= limit:
            break
    return out


def risk_alerts(con: duckdb.DuckDBPyConnection, known_txn: set[int], limit: int) -> list[dict]:
    """The highest bank-risk-score transactions that were never sampled into the 20 cases."""
    excl = ",".join(map(str, known_txn)) or "-1"
    df = con.execute(f"""
        SELECT f.tid, f.card_id, f.ts, t.risk_score FROM feat f JOIN tx t ON t.TransactionID = f.tid
        WHERE t.risk_score IS NOT NULL AND f.tid NOT IN ({excl})
        ORDER BY t.risk_score DESC LIMIT {limit * 4}""").df()
    seen_cards: set[str] = set()
    out = []
    for r in df.to_dict("records"):                          # at most one alert per card, highest risk score first
        if r["card_id"] in seen_cards:
            continue
        seen_cards.add(r["card_id"])
        out.append({"tid": int(r["tid"]), "card_id": r["card_id"], "ts": str(r["ts"]), "reason": f"bank risk score {r['risk_score']:.2f}"})
        if len(out) >= limit:
            break
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="cases_extra")
    ap.add_argument("--rings", type=int, default=5)
    ap.add_argument("--risk", type=int, default=5)
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()

    backend, memory, name = make_backend()
    print(f"backend: {name}")
    if not hasattr(backend, "find_rings"):
        raise SystemExit("the autonomous monitor needs the TigerGraph backend (GRAPHSLEUTH_BACKEND=tigergraph): "
                          "find_rings/device_neighbors are graph-wide sweeps, not answered by the local store")
    llm = None if a.no_llm else LLMRouter.from_env()
    print("llm: " + (", ".join(llm.providers) if llm and llm.available else "off (no API keys; template narratives)"))
    rag = make_rag(backend, llm)
    orch = Orchestrator(backend, None, llm=llm, rag=rag)      # memory=None: monitor cases are never written back to the graph

    data_dir = os.getenv("DATA_DIR", "../dataset/HHGOA_IEEE")
    pack = pd.read_csv(f"{data_dir}/case_pack.csv")
    known_cards, known_txn = set(pack["card_id"]), set(pack["flagged_txn_id"])
    con = duckdb.connect(str(DB), read_only=True)
    customer_of = {r[0]: r[1] for r in con.execute("SELECT card_id, customer_id FROM cards").fetchall()}

    triggers = []
    for r in ring_alerts(backend, known_cards, a.rings):
        triggers.append({"trigger_type": "risk_score", "card_id": r["card_id"], "customer_id": customer_of.get(r["card_id"], ""),
                         "flagged_txn_id": _latest_txn(con, r["card_id"], r["flagged_ts"]), "opened_at": r["flagged_ts"],
                         "monitor_reason": r["reason"]})
    for r in risk_alerts(con, known_txn, a.risk):
        triggers.append({"trigger_type": "risk_score", "card_id": r["card_id"], "customer_id": customer_of.get(r["card_id"], ""),
                         "flagged_txn_id": r["tid"], "opened_at": r["ts"], "monitor_reason": r["reason"]})

    out = Path(a.out)
    out.mkdir(exist_ok=True)
    n_ring = sum(1 for t in triggers if "ring device" in t["monitor_reason"])
    print(f"{len(triggers)} monitor alerts ({n_ring} from the ring sweep, {len(triggers) - n_ring} from risk triage; "
          f"requested up to {a.rings}/{a.risk})")
    for i, t in enumerate(triggers, start=101):               # MON-101.. : outside 001-020, can never collide with an HHG case id
        case_id = f"MON-{i}"
        trig = {"case_id": case_id, "trigger_type": t["trigger_type"], "card_id": t["card_id"], "customer_id": t["customer_id"],
                "flagged_txn_id": t["flagged_txn_id"], "opened_at": t["opened_at"]}
        if not trig["flagged_txn_id"] or not trig["customer_id"]:
            print(f"{case_id} skipped: no transaction/customer resolved for {t['card_id']}")
            continue
        ans = orch.run_case(trig, write_memory=False)
        (out / f"{case_id}.json").write_text(json.dumps({**ans.model_dump(), "monitor_reason": t["monitor_reason"]}, indent=2) + "\n")
        c = ans.case
        fin = ",".join(x.action for x in ans.next_best_actions.final)
        print(f"{case_id} [{t['monitor_reason'][:28]:28}] {c.verdict[:5]:5} p={c.fraud_probability:.2f} {c.pattern[:16]:16} "
              f"${c.exposure_usd:>8.2f} sar={str(ans.sar.file):5} -> {fin}")


def _latest_txn(con: duckdb.DuckDBPyConnection, card_id: str, before_ts: str) -> int | None:
    r = con.execute("SELECT tid FROM feat WHERE card_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1", [card_id, before_ts]).fetchone()
    return int(r[0]) if r else None


if __name__ == "__main__":
    main()
