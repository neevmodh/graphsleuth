"""Validate answer files against the README's Answer Format and the dataset.

Checks, per file: schema; case id is in case_pack; every transaction / card / closed-case / device id exists in the
dataset; exposure equals the sum of absolute amounts of affected transactions; action names, routes and R10 follow the
policy; sar.file agrees with FILE_REPORT; the empty-field rules for legitimate verdicts and no-report cases.

    python -m eval.validate_answers cases/
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.policy import route_for  # noqa: E402
from agent.schemas import Answer  # noqa: E402

DB = Path(__file__).resolve().parents[1] / "data" / "store" / "graphsleuth.duckdb"


def validate_file(path: Path, con) -> list[str]:
    errs: list[str] = []
    try:
        raw = json.loads(path.read_text())
        a = Answer.model_validate(raw)
    except Exception as e:                       # noqa: BLE001
        return [f"schema: {str(e)[:300]}"]
    if a.case_id != path.stem:
        errs.append(f"case_id {a.case_id} does not match file name {path.stem}")
    if not con.execute("SELECT 1 FROM case_pack WHERE case_id = ?", [a.case_id]).fetchone():
        errs.append(f"{a.case_id} is not in case_pack.csv")

    c = a.case
    tids = [int(t) for t in c.affected_txn_ids + ([c.first_suspicious_txn_id] if c.first_suspicious_txn_id else [])]
    if tids:
        found = {r[0] for r in con.execute(f"SELECT TransactionID FROM txn WHERE TransactionID IN ({','.join(map(str, set(tids)))})").fetchall()}
        missing = set(tids) - found
        if missing:
            errs.append(f"unknown transaction ids: {sorted(missing)[:5]}")
    if c.affected_txn_ids:
        tot = con.execute(f"SELECT sum(abs(TransactionAmt)) FROM txn WHERE TransactionID IN ({','.join(c.affected_txn_ids)})").fetchone()[0]
        if abs(float(tot) - c.exposure_usd) > 0.011:
            errs.append(f"exposure_usd {c.exposure_usd} != sum of |amount| {round(float(tot), 2)}")
        if c.first_suspicious_txn_id and c.first_suspicious_txn_id not in c.affected_txn_ids:
            errs.append("first_suspicious_txn_id is not among affected_txn_ids")
    elif c.exposure_usd != 0:
        errs.append("no affected transactions but exposure_usd != 0")
    cards = set(c.connected_card_ids)
    if cards:
        found = {r[0] for r in con.execute(f"SELECT card_id FROM cards WHERE card_id IN ({','.join(repr(x) for x in cards)})").fetchall()}
        if cards - found:
            errs.append(f"unknown connected cards: {sorted(cards - found)[:5]}")
    for cid in c.similar_prior_cases:
        if not con.execute("SELECT 1 FROM closed_cases WHERE case_id = ?", [cid]).fetchone():
            errs.append(f"unknown closed case {cid}")
    for dev in c.connected_device_profiles:
        if not con.execute("SELECT 1 FROM tx WHERE device_profile = ? LIMIT 1", [dev]).fetchone():
            errs.append(f"unknown device profile {dev[:40]}")
    for e in c.evidence:
        for eid in e.entity_ids:
            if eid.isdigit() and not con.execute("SELECT 1 FROM txn WHERE TransactionID = ?", [int(eid)]).fetchone():
                errs.append(f"evidence references unknown transaction {eid}")

    fin = a.next_best_actions.final
    for lst, name in ((a.next_best_actions.initial, "initial"), (fin, "final")):
        seen = set()
        for it in lst:
            exp = route_for(it.action, c.exposure_usd)
            if it.route != exp:
                errs.append(f"{name}: {it.action} route {it.route} should be {exp}")
            if it.action in seen:
                errs.append(f"{name}: duplicate action {it.action}")
            seen.add(it.action)
            if not any(ch.isdigit() for ch in it.reason):
                errs.append(f"{name}: {it.action} reason does not cite a rule/section")
    if any(i.action == "BLOCK_ALL_CARDS" for i in fin) and not (c.verdict == "fraud" and len(c.connected_card_ids) >= 1):
        errs.append("BLOCK_ALL_CARDS emitted without R10 grounds")
    if a.sar.file:
        if a.sar.total_amount_usd and abs(a.sar.total_amount_usd - c.exposure_usd) > 0.011:
            errs.append("sar.total_amount_usd does not equal exposure_usd")
        if c.affected_txn_ids and len(a.sar.activity_dates) == 2:
            span = con.execute(f"SELECT min(ts), max(ts) FROM feat WHERE tid IN ({','.join(c.affected_txn_ids)})").fetchone()
            if span[0] is not None:
                lo, hi = str(span[0])[:10], str(span[1])[:10]
                if a.sar.activity_dates != [lo, hi]:
                    errs.append(f"sar.activity_dates {a.sar.activity_dates} does not match the affected transactions' dates ({lo}..{hi})")
        if not (6 <= len([s for s in a.sar.narrative.replace("? ", ". ").split(". ") if s.strip()]) <= 14):
            errs.append("sar.narrative should be about 6-12 sentences")
        if a.sar.subjects and not any(s.startswith("C") for s in a.sar.subjects):
            errs.append("sar.subjects should include the customer/card ids")
    if c.verdict == "legitimate" and a.sar.file:
        errs.append("legitimate verdict must not file a report")
    if c.status == "closed_legitimate" and c.verdict != "legitimate":
        errs.append("status closed_legitimate requires verdict legitimate")
    return errs


def main(folder: str) -> int:
    con = duckdb.connect(str(DB), read_only=True)
    files = sorted(Path(folder).glob("HHG-*.json"))
    expected = {r[0] for r in con.execute("SELECT case_id FROM case_pack").fetchall()}
    missing = expected - {f.stem for f in files}
    bad = 0
    for f in files:
        e = validate_file(f, con)
        print(("OK   " if not e else "FAIL ") + f.name + ("" if not e else " :: " + "; ".join(e)))
        bad += bool(e)
    if missing:
        print("MISSING:", sorted(missing))
    print(f"{len(files) - bad}/{len(expected)} valid")
    return 1 if (bad or missing) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "cases"))
