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

from agent.backend import LocalBackend, _clean
from agent.memory import LocalCaseMemory
from agent.orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "cases"
APPROVALS = ROOT / "data" / "store" / "approvals.json"

app = FastAPI(title="GraphSleuth")
_lock = threading.Lock()
_memory = LocalCaseMemory()
_pack: dict[str, dict] = {}


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


@app.get("/api/meta")
def meta():
    return {"backend": "local-duckdb", "tigergraph": False, "cases": len(pack())}


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
    return {"trigger": pack()[cid], "answer": cached(cid), "approvals": load_approvals().get(cid, {})}


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
            orch = Orchestrator(LocalBackend("final"), _memory)

            def on_step(s) -> None:
                push("step", {"n": s.n, "tool": s.tool, "summary": s.summary, "ms": round(s.ms, 1)})
                if pace:
                    time.sleep(pace)

            with _lock:                       # one writer to the local memory store at a time
                ans = orch.run_case(trig, on_step=on_step)
            data = ans.model_dump()
            CASES.mkdir(exist_ok=True)
            (CASES / f"{cid}.json").write_text(json.dumps(data, indent=2) + "\n")
            push("done", {"answer": data, "window": window_payload(orch), "graph": graph_payload(cid, data, orch)})
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
    return ap[cid][d.action]


@app.exception_handler(Exception)
async def _err(_, exc: Exception):
    return JSONResponse({"error": str(exc)}, status_code=500)


app.mount("/", StaticFiles(directory=str(ROOT / "ui"), html=True), name="ui")
