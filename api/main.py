"""GraphSleuth API: serves the analyst UI, streams live investigations (SSE) and records human approvals.

    uvicorn api.main:app --port 8000        then open http://localhost:8000
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from dataclasses import asdict

from agent import counterfactual, decision_gate, mock_actions
from agent.backend import LocalBackend, _clean
from agent.backend_factory import make_backend, make_rag
from agent.investigator import Investigation
from agent.memory import LocalCaseMemory
from agent.llm import LLMRouter
from agent.orchestrator import Orchestrator
from agent.policy import CASE_GATE_P, STOP_HIGH_P, STOP_LOW_P

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "cases"
APPROVALS = ROOT / "data" / "store" / "approvals.json"
ACTIONS_LOG = ROOT / "data" / "store" / "actions_taken.json"

app = FastAPI(title="GraphSleuth")
_lock = threading.Lock()
import os

TOKEN_BUDGET = int(os.getenv("GRAPHSLEUTH_TOKEN_BUDGET", "150000"))   # soft session budget, purely for the UI's fuel gauge

_TG = os.getenv("GRAPHSLEUTH_BACKEND", "local").lower() == "tigergraph"
_shared = make_backend() if _TG else None          # TigerGraph connections are shared; DuckDB ones are per request (thread safety)
_memory = _shared[1] if _TG else LocalCaseMemory()
_llm = LLMRouter.from_env()
_rag = make_rag(_shared[0], _llm) if _TG else None    # GraphRAG needs the TigerGraph backend; opened once, reused per request
_pack: dict[str, dict] = {}
_investigations: dict[str, Investigation] = {}      # last run's Investigation per case, for counterfactuals/uncertainty (in-memory only)


def pack() -> dict[str, dict]:
    if not _pack:
        b = LocalBackend("final")
        for r in b.query("SELECT * FROM case_pack ORDER BY case_id").to_dict("records"):
            r["opened_at"] = str(r["opened_at"])
            _pack[r["case_id"]] = _clean(r)
    return _pack


def cached(cid: str) -> dict | None:
    f = CASES / f"{cid}.json"
    return json.loads(f.read_text()) if f.exists() else None


def load_approvals() -> dict:
    return json.loads(APPROVALS.read_text()) if APPROVALS.exists() else {}


def load_actions_log() -> dict:
    return json.loads(ACTIONS_LOG.read_text()) if ACTIONS_LOG.exists() else {}


def record_actions(cid: str, receipts: list[dict]) -> list[dict]:
    """Record mock-execution receipts for this case, keyed by action so a re-run or a re-approval
    replaces its own receipt (fresh timestamp) instead of piling up duplicates."""
    log = load_actions_log()
    bucket = log.setdefault(cid, {})
    for r in receipts:
        bucket[r["action"]] = r
    ACTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    ACTIONS_LOG.write_text(json.dumps(log, indent=2))
    return list(bucket.values())


def execute_auto_actions(cid: str, card_id: str, answer: dict) -> list[dict]:
    """Only 'auto' route actions execute without a human; run them against the mock action
    layer right after the case is written, so the case record and the actions log stay in sync."""
    autos = [a for a in answer["next_best_actions"]["final"] if a["route"] == "auto"]
    receipts = [mock_actions.execute(a["action"], cid, card_id, a["route"]) for a in autos]
    return record_actions(cid, receipts)


@app.get("/api/meta")
def meta():
    used = _llm.tokens
    return {
        "backend": "tigergraph" if _TG else "local-duckdb", "tigergraph": _TG, "llm": list(_llm.providers), "cases": len(pack()),
        "usage": {
            "tokens_used": used, "tokens_budget": TOKEN_BUDGET, "tokens_left": max(TOKEN_BUDGET - used, 0),
            "pct_used": round(min(used / TOKEN_BUDGET, 1.0) * 100, 1) if TOKEN_BUDGET else 0,
            "calls": _llm.calls, "cache_hits": _llm.cache_hits,
            "cache_hit_rate": round(_llm.cache_hits / _llm.calls * 100, 1) if _llm.calls else 0,
        },
    }


@app.get("/api/cases")
def list_cases():
    out = []
    for cid, t in pack().items():
        a = cached(cid)
        row = {**t}
        if a:
            c = a["case"]
            row.update(verdict=c["verdict"], p=c["fraud_probability"], pattern=c["pattern"], exposure=c["exposure_usd"],
                       status=c["status"], sar=a["sar"]["file"])
        out.append(row)
    return out


@app.get("/api/cases/{cid}")
def get_case(cid: str):
    if cid not in pack():
        raise HTTPException(404, "unknown case")
    return {"trigger": pack()[cid], "answer": cached(cid), "approvals": load_approvals().get(cid, {}),
            "actions_taken": list(load_actions_log().get(cid, {}).values())}


def window_payload(orch: Orchestrator) -> list[dict]:
    inv = orch.last
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


def graph_payload(cid: str, answer: dict, orch: Orchestrator | None) -> dict:
    """Nodes/edges for the evidence graph: customer, card, its episode, the shared device and connected cards."""
    t = pack()[cid]
    c = answer["case"]
    nodes = [{"id": t["customer_id"], "label": t["customer_id"], "type": "customer"},
             {"id": t["card_id"], "label": t["card_id"], "type": "card", "focus": True}]
    edges = [{"s": t["customer_id"], "t": t["card_id"], "label": "OWNS"}]
    for tid in c["affected_txn_ids"][:12]:
        nodes.append({"id": f"T{tid}", "label": tid[-5:], "type": "txn", "title": tid})
        edges.append({"s": t["card_id"], "t": f"T{tid}", "label": "MADE"})
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
        edges.append({"s": t["card_id"], "t": cs, "label": "SIMILAR_TO"})
    seen, uniq = set(), []
    for n in nodes:
        if n["id"] not in seen:
            seen.add(n["id"]); uniq.append(n)
    return {"nodes": uniq, "edges": edges}


@app.get("/api/cases/{cid}/run")
async def run_case(cid: str, pace: float = 0.0):
    """Run the investigation live; one SSE `step` event per tool call, then `done` with the full answer."""
    if cid not in pack():
        raise HTTPException(404, "unknown case")
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    trig = pack()[cid]

    def push(kind: str, data) -> None:
        loop.call_soon_threadsafe(q.put_nowait, (kind, data))

    def work() -> None:
        try:
            orch = Orchestrator(_shared[0] if _TG else LocalBackend("final"), _memory, llm=_llm, rag=_rag)

            def on_step(s) -> None:
                push("step", {"n": s.n, "tool": s.tool, "summary": s.summary, "ms": round(s.ms, 1)})
                if pace:
                    time.sleep(pace)

            with _lock:                       # one writer to the local memory store at a time
                ans = orch.run_case(trig, on_step=on_step)
            _investigations[cid] = orch.last   # kept in memory for /counterfactuals; never persisted
            data = ans.model_dump()
            CASES.mkdir(exist_ok=True)
            (CASES / f"{cid}.json").write_text(json.dumps(data, indent=2) + "\n")
            actions_taken = execute_auto_actions(cid, trig["card_id"], data)
            push("done", {"answer": data, "window": window_payload(orch), "graph": graph_payload(cid, data, orch),
                          "actions_taken": actions_taken})
        except Exception as e:                # noqa: BLE001
            push("error", {"message": str(e)})

    threading.Thread(target=work, daemon=True).start()

    async def gen():
        while True:
            kind, data = await q.get()
            yield {"event": kind, "data": json.dumps(data)}
            if kind in ("done", "error"):
                break

    return EventSourceResponse(gen())


@app.get("/api/cases/{cid}/graph")
def case_graph(cid: str):
    a = cached(cid)
    if not a:
        raise HTTPException(404, "run the case first")
    return graph_payload(cid, a, None)


@app.get("/api/cases/{cid}/counterfactuals")
def case_counterfactuals(cid: str):
    """#27: which evidence signal, if it had come out differently, would have flipped the verdict -- and how close
    the case sits to the nearest policy threshold (the UI's uncertainty read-out). Computed from the live
    Investigation kept from the last /run, so investigate the case first."""
    inv = _investigations.get(cid)
    if inv is None:
        raise HTTPException(404, "run the case first: counterfactuals are computed from the live investigation, not the saved answer")
    thresholds = [("legitimate", STOP_LOW_P), ("case-opens", CASE_GATE_P), ("fraud", STOP_HIGH_P)]
    nearest = min(thresholds, key=lambda kv: abs(inv.p_case - kv[1]))
    return {
        "case_id": cid, "fraud_probability": round(inv.p_case, 3),
        "uncertainty": {"nearest_threshold": nearest[0], "threshold_value": nearest[1],
                        "distance": round(abs(inv.p_case - nearest[1]), 3)},
        "counterfactuals": [asdict(c) for c in counterfactual.explain(inv)],
    }


@app.get("/api/cases/{cid}/decision_gate")
def case_decision_gate(cid: str):
    """Explicit pass/fail checklist for the same conditions the policy engine's stop rule already evaluates
    (case gate, corroboration count, belief conflict, stop threshold) -- a read-only view, computed from the
    live Investigation kept from the last /run, so investigate the case first."""
    inv = _investigations.get(cid)
    if inv is None:
        raise HTTPException(404, "run the case first: the decision gate is computed from the live investigation, not the saved answer")
    gate = decision_gate.evaluate(inv)
    if gate is None:
        raise HTTPException(404, "no assessment available for this investigation")
    return {"case_id": cid, **asdict(gate)}


class Decision(BaseModel):
    action: str
    decision: str          # approved | rejected
    note: str = ""


@app.post("/api/cases/{cid}/approve")
def approve(cid: str, d: Decision):
    a = cached(cid)
    if not a:
        raise HTTPException(404, "run the case first")
    final = {x["action"]: x for x in a["next_best_actions"]["final"]}
    if d.action not in final:
        raise HTTPException(400, "action is not in the final recommendation")
    if final[d.action]["route"] == "auto":
        raise HTTPException(400, "auto actions need no approval")
    if d.decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision must be approved or rejected")
    ap = load_approvals()
    ap.setdefault(cid, {})[d.action] = {"decision": d.decision, "route": final[d.action]["route"], "note": d.note,
                                        "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    APPROVALS.parent.mkdir(parents=True, exist_ok=True)
    APPROVALS.write_text(json.dumps(ap, indent=2))
    if d.decision == "approved":                  # an approved L1/L2 action now actually fires, same as an auto action
        record_actions(cid, [mock_actions.execute(d.action, cid, pack()[cid]["card_id"], final[d.action]["route"])])
    return ap[cid][d.action]


@app.exception_handler(Exception)
async def _err(_, exc: Exception):
    return JSONResponse({"error": str(exc)}, status_code=500)


app.mount("/", StaticFiles(directory=str(ROOT / "ui"), html=True), name="ui")
