"""Load the exported CSVs (data/store/tg/) into the GraphSleuth graph on TigerGraph through the REST API.

    python -m data.load_tg              # everything
    python -m data.load_tg --limit 5000 # pilot: first 5000 transactions (and only their cards/devices)
    python -m data.load_tg --only closed

Upserts are idempotent, so a rerun (or a resume after a network error) is safe. No cloud storage is needed.
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
SRC = ROOT / "data" / "store" / "tg"
GRAPH = "GraphSleuth"

TXN_ATTRS = ["ts", "amt", "prod", "channel", "addr1", "bank_risk", "p", "p_oof", "dev_new", "dev_unseen", "addr1_new", "prod_new",
             "via_proxy", "amt_ratio_med", "gap_prev_h", "n_1h", "n_small_1h", "addr1_prior_n", "prior_n",
             "pat_ato", "pat_cnp", "pat_cnpnd", "pat_oor", "pat_none", "card_id"]


def connect():
    from pyTigerGraph import TigerGraphConnection
    c = TigerGraphConnection(host=os.environ["TG_HOST"], graphname=GRAPH, gsqlSecret=os.environ["TG_SECRET"], tgCloud=True)
    c.getToken(os.environ["TG_SECRET"])
    return c


def chunks(df: pd.DataFrame, n: int):
    for i in range(0, len(df), n):
        yield df.iloc[i:i + n]


def retry(fn, tries: int = 4):
    for k in range(tries):
        try:
            return fn()
        except Exception:                        # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(2 ** k)


def vertices(conn, df, vtype, vid, attrs, size=5000, desc=None):
    for part in tqdm(list(chunks(df, size)), desc=desc or vtype, leave=False):
        retry(lambda p=part: conn.upsertVertexDataFrame(p, vtype, v_id=vid, attributes={a: a for a in attrs}))


def edges(conn, df, src_t, etype, dst_t, src_col, dst_col, size=8000, desc=None):
    df = df[[src_col, dst_col]]        # the edges carry no attributes; extra columns would be sent as (invalid) attributes
    for part in tqdm(list(chunks(df, size)), desc=desc or etype, leave=False):
        retry(lambda p=part: conn.upsertEdgeDataFrame(p, src_t, etype, dst_t, from_id=src_col, to_id=dst_col, attributes={}))


def count(conn, vtype):
    return conn.getVertexCount(vtype)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", choices=["static", "txn", "closed"], default=None)
    a = ap.parse_args()
    conn = connect()
    t0 = time.time()

    txn = pd.read_csv(SRC / "transactions.csv", dtype={"card_id": str, "device_profile": str, "email": str, "region": str, "next_tid": str},
                      nrows=a.limit, keep_default_na=False, na_values=[""])
    txn = txn.rename(columns={"proxy": "via_proxy"})
    if a.limit:
        print(f"pilot: {len(txn):,} transactions")

    if a.only in (None, "static"):
        cards = pd.read_csv(SRC / "cards.csv", keep_default_na=False)
        devices = pd.read_csv(SRC / "devices.csv", keep_default_na=False)
        if a.limit:
            cards = cards[cards.card_id.isin(txn.card_id)]
            devices = devices[devices.iloc[:, 0].isin(txn.device_profile.dropna())]
        vertices(conn, pd.DataFrame({"id": cards.customer_id.unique()}), "Customer", "id", [], desc="Customer")
        vertices(conn, cards.rename(columns={"card_id": "id"}), "Card", "id", ["customer_id", "card4", "card6"], desc="Card")
        edges(conn, cards, "Customer", "OWNS", "Card", "customer_id", "card_id", desc="OWNS")
        dv = devices.copy(); dv.columns = ["id", "n_cards", "new_frac", "proxy_frac"]
        vertices(conn, dv, "DeviceProfile", "id", ["n_cards", "new_frac", "proxy_frac"], desc="DeviceProfile")
        regions = pd.read_csv(SRC / "regions.csv"); regions.columns = ["addr1"]
        vertices(conn, regions, "BillingRegion", "addr1", [], desc="BillingRegion")
        em = pd.read_csv(SRC / "emails.csv", keep_default_na=False); em.columns = ["domain"]
        vertices(conn, em, "EmailDomain", "domain", [], desc="EmailDomain")
        print(f"static done  ({time.time() - t0:.0f}s)", flush=True)

    if a.only in (None, "txn"):
        vertices(conn, txn, "Transaction", "tid", TXN_ATTRS, size=4000, desc="Transaction")
        print(f"transactions done ({time.time() - t0:.0f}s)", flush=True)
        edges(conn, txn, "Card", "MADE", "Transaction", "card_id", "tid", desc="MADE")
        d = txn[txn.device_profile.notna()]
        edges(conn, d, "Transaction", "FROM_DEVICE", "DeviceProfile", "tid", "device_profile", desc="FROM_DEVICE")
        e = txn[txn.email.notna()]
        edges(conn, e, "Transaction", "PURCHASER_EMAIL", "EmailDomain", "tid", "email", desc="PURCHASER_EMAIL")
        r = txn[txn.region.notna()]
        edges(conn, r, "Transaction", "BILLED_IN", "BillingRegion", "tid", "region", desc="BILLED_IN")
        n = txn[txn.next_tid.notna()]
        n = n.assign(next_tid=n.next_tid.astype("int64"))
        edges(conn, n, "Transaction", "NEXT", "Transaction", "tid", "next_tid", desc="NEXT")
        print(f"transaction edges done ({time.time() - t0:.0f}s)", flush=True)

    if a.only in (None, "closed"):
        cc = pd.read_csv(SRC / "closed_cases.csv", keep_default_na=False)
        cc["report_filed"] = cc["report_filed"].astype(str).str.lower().eq("true")
        cc = cc.rename(columns={"case_id": "id", "exposure_usd": "exposure"})
        vertices(conn, cc, "ClosedCase", "id", ["outcome", "pattern", "exposure", "opened_at", "closed_at", "report_filed", "notes"], size=1000, desc="ClosedCase")
        edges(conn, cc, "ClosedCase", "ON_CARD", "Card", "id", "card_id", desc="ON_CARD")
        ct = pd.read_csv(SRC / "closed_case_txn.csv")
        edges(conn, ct, "ClosedCase", "INVOLVES", "Transaction", "case_id", "tid", desc="INVOLVES")
        cn = pd.read_csv(SRC / "closed_case_conn.csv", keep_default_na=False)
        edges(conn, cn, "ClosedCase", "CONNECTED_TO", "Card", "case_id", "card_id", desc="CONNECTED_TO")
        print(f"closed cases done ({time.time() - t0:.0f}s)", flush=True)

    for v in ["Customer", "Card", "Transaction", "DeviceProfile", "BillingRegion", "EmailDomain", "ClosedCase"]:
        print(f"  {v:14} {count(conn, v):>9,}")


if __name__ == "__main__":
    main()
