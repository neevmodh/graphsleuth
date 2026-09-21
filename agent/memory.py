"""Case memory: write every investigation so the next one can find it.

`CaseMemory` is the interface; `LocalCaseMemory` keeps cases in a small DuckDB file (used for development and
when TigerGraph is unavailable, reported with written_to_graph=False). The TigerGraph implementation in
agent/tg_backend.py writes a Case vertex with edges to the card, transactions, devices and similar cases.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

import duckdb

from .schemas import Case

MEM_DB = Path(__file__).resolve().parents[1] / "data" / "store" / "memory.duckdb"


class CaseMemory(Protocol):
    def write(self, trig: dict, case: Case, inv, final_actions) -> tuple[str, bool]: ...
    def find(self, card_id: str | None = None, device_profile: str | None = None) -> list[dict]: ...


class LocalCaseMemory:
    graph = False   # not TigerGraph

    def __init__(self, path: Path = MEM_DB):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path))
        self.con.execute("""CREATE TABLE IF NOT EXISTS agent_cases (
            graph_case_id VARCHAR PRIMARY KEY, case_id VARCHAR, card_id VARCHAR, customer_id VARCHAR, device_profile VARCHAR,
            verdict VARCHAR, pattern VARCHAR, fraud_probability DOUBLE, exposure_usd DOUBLE, status VARCHAR,
            txn_ids VARCHAR, connected_cards VARCHAR, actions VARCHAR, summary VARCHAR, created TIMESTAMP DEFAULT now())""")

    def write(self, trig, case: Case, inv, final_actions) -> tuple[str, bool]:
        prev = self.con.execute("SELECT graph_case_id FROM agent_cases WHERE case_id = ?", [trig["case_id"]]).fetchone()
        if prev:
            gid = prev[0]                    # re-running a case updates its record instead of creating a new one
        else:
            n = self.con.execute("SELECT coalesce(max(CAST(right(graph_case_id, 4) AS INTEGER)), 1000) FROM agent_cases").fetchone()[0] + 1
            gid = f"CASE-2016-{n:04d}"
        dev = case.connected_device_profiles[0] if case.connected_device_profiles else inv.flagged.get("device_profile")
        self.con.execute("DELETE FROM agent_cases WHERE case_id = ?", [trig["case_id"]])
        self.con.execute("INSERT INTO agent_cases (graph_case_id, case_id, card_id, customer_id, device_profile, verdict, pattern, "
                         "fraud_probability, exposure_usd, status, txn_ids, connected_cards, actions, summary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         [gid, trig["case_id"], trig["card_id"], trig["customer_id"], dev, case.verdict, case.pattern,
                          case.fraud_probability, case.exposure_usd, case.status, "|".join(case.affected_txn_ids),
                          "|".join(case.connected_card_ids), json.dumps([a.action for a in final_actions]), case.summary])
        return gid, False

    def find(self, card_id=None, device_profile=None) -> list[dict]:
        q, args = "SELECT * FROM agent_cases WHERE ", []
        cond = []
        if card_id:
            cond.append("card_id = ?"); args.append(card_id)
        if device_profile:
            cond.append("device_profile = ?"); args.append(device_profile)
        if not cond:
            return []
        return self.con.execute(q + " OR ".join(cond) + " ORDER BY created DESC", args).df().to_dict("records")
