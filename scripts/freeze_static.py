"""Freeze a read-only static snapshot of all 20 cases (+ the autonomous monitor's) for the Vercel demo:
window/graph/counterfactuals/steps come from a real run against the live TigerGraph + LLM + GraphRAG backend (so the
demo shows exactly the same ring_component/graphrag_retrieve steps the live UI does), the case/evidence/actions/SAR
text comes from the already-committed, hand-reviewed cases/*.json (never overwritten here -- this script only reads
it, to keep the frozen bundle byte-identical to the graded answer).

    GRAPHSLEUTH_BACKEND=tigergraph python scripts/freeze_static.py

Writes static_demo/data/<CASE_ID>.json (trigger, answer, window, graph, counterfactuals, steps) and
static_demo/data/index.json (the queue summary, mirrors GET /api/cases).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from agent import counterfactual
from agent.backend import _clean
from agent.backend_factory import make_backend, make_rag
from agent.llm import LLMRouter
from agent.orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "cases"
CASES_EXTRA = ROOT / "cases_extra"
OUT = ROOT / "static_demo" / "data"


def window_payload(inv) -> list[dict]:
    if inv is None or inv.window is None:
        return []
    w = inv.window[["tid", "ts", "amt", "channel", "p_oof"]].copy()
    if inv.episode_scores is not None:
        w = w.merge(inv.episode_scores[["tid", "m"]], on="tid", how="left")
    else:
        w["m"] = None
    w["in_episode"] = w["tid"].isin(inv.episode)
    w["flagged"] = w["tid"] == int(inv.trigger["flagged_txn_id"])
    w["ts"] = w["ts"].astype(str)
    return _clean(w.rename(columns={"p_oof": "p"}).to_dict("records"))


def graph_payload(trig: dict, answer: dict) -> dict:
    c = answer["case"]
    nodes = [{"id": trig["customer_id"], "label": trig["customer_id"], "type": "customer"},
             {"id": trig["card_id"], "label": trig["card_id"], "type": "card", "focus": True}]
    edges = [{"s": trig["customer_id"], "t": trig["card_id"], "label": "OWNS"}]
    for tid in c["affected_txn_ids"][:12]:
        nodes.append({"id": f"T{tid}", "label": tid[-5:], "type": "txn", "title": tid})
        edges.append({"s": trig["card_id"], "t": f"T{tid}", "label": "MADE"})
    dev = (c["connected_device_profiles"] or [None])[0]
    if dev:
        nodes.append({"id": "DEV", "label": "shared device", "type": "device", "title": dev})
        for tid in c["affected_txn_ids"][:12]:
            edges.append({"s": f"T{tid}", "t": "DEV", "label": "FROM_DEVICE"})
        for cc in c["connected_card_ids"][:30]:
            nodes.append({"id": cc, "label": cc, "type": "connected"})
            edges.append({"s": cc, "t": "DEV", "label": "ON_DEVICE"})
    for cs in c["similar_prior_cases"]:
        nodes.append({"id": cs, "label": cs, "type": "closedcase"})
        edges.append({"s": trig["card_id"], "t": cs, "label": "SIMILAR_TO"})
    seen, uniq = set(), []
    for n in nodes:
        if n["id"] not in seen:
            seen.add(n["id"]); uniq.append(n)
    return {"nodes": uniq, "edges": edges}


def steps_payload(inv) -> list[dict]:
    if inv is None:
        return []
    return [{"n": s.n, "tool": s.tool, "summary": s.summary, "ms": round(s.ms, 1)} for s in inv.steps]


def counterfactuals_payload(inv) -> dict | None:
    if inv is None:
        return None
    from agent.policy import CASE_GATE_P, STOP_HIGH_P, STOP_LOW_P
    thresholds = [("legitimate", STOP_LOW_P), ("case-opens", CASE_GATE_P), ("fraud", STOP_HIGH_P)]
    nearest = min(thresholds, key=lambda kv: abs(inv.p_case - kv[1]))
    return {
        "case_id": inv.trigger["case_id"], "fraud_probability": round(inv.p_case, 3),
        "uncertainty": {"nearest_threshold": nearest[0], "threshold_value": nearest[1],
                        "distance": round(abs(inv.p_case - nearest[1]), 3)},
        "counterfactuals": [asdict(c) for c in counterfactual.explain(inv)],
    }


def freeze_one(orch: Orchestrator, trig: dict, answer: dict) -> dict:
    orch.run_case(dict(trig), write_memory=False)   # populates orch.last; its own `answer` is discarded, cases/*.json is authoritative
    inv = orch.last
    return {
        "trigger": trig, "answer": answer,
        "window": window_payload(inv), "graph": graph_payload(trig, answer),
        "steps": steps_payload(inv), "counterfactuals": counterfactuals_payload(inv),
    }


def main() -> None:
    assert os.getenv("GRAPHSLEUTH_BACKEND", "").lower() == "tigergraph", "run with GRAPHSLEUTH_BACKEND=tigergraph"
    backend, memory, name = make_backend()
    llm = LLMRouter.from_env()
    rag = make_rag(backend, llm)
    print(f"backend: {name}, llm: {', '.join(llm.providers) if llm else None}, rag: {rag is not None}")
    orch = Orchestrator(backend, None, llm=llm, rag=rag)

    data_dir = os.getenv("DATA_DIR", "../dataset/HHGOA_IEEE")
    pack = pd.read_csv(f"{data_dir}/case_pack.csv").set_index("case_id")
    OUT.mkdir(parents=True, exist_ok=True)

    index = []
    for f in sorted(CASES.glob("HHG-*.json")):
        cid = f.stem
        answer = json.loads(f.read_text())
        trig = pack.loc[cid].to_dict()
        trig["case_id"] = cid
        trig["opened_at"] = str(trig["opened_at"])
        bundle = freeze_one(orch, trig, answer)
        (OUT / f"{cid}.json").write_text(json.dumps(bundle, indent=2, default=str) + "\n")
        c = answer["case"]
        index.append({"case_id": cid, "trigger_type": trig["trigger_type"], "opened_at": trig["opened_at"],
                      "verdict": c["verdict"], "p": c["fraud_probability"], "sar": bool(answer["sar"]["file"])})
        print(f"{cid} frozen: {len(bundle['steps'])} steps, {len(bundle['window'])} window txns, "
              f"{len(bundle['counterfactuals']['counterfactuals']) if bundle['counterfactuals'] else 0} counterfactuals")

    (OUT / "index.json").write_text(json.dumps(index, indent=2) + "\n")

    # autonomous monitor: static content straight from the committed cases_extra/ (no re-run -- these are exploratory,
    # never written back to the graph, and the monitor_reason/trigger shape differs from the 20 benchmark cases)
    monitor = []
    for f in sorted(CASES_EXTRA.glob("MON-*.json")):
        monitor.append(json.loads(f.read_text()))
    (OUT / "monitor.json").write_text(json.dumps(monitor, indent=2) + "\n")
    print(f"monitor: {len(monitor)} cases frozen")


if __name__ == "__main__":
    main()
