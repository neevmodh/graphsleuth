"""Build the local DuckDB store from the provided CSVs.

The graph (TigerGraph) holds the investigation-relevant subset; this store keeps
every original column for feature engineering, evaluation and fast lookups.
Only the provided dataset is used (never the public Kaggle IEEE-CIS files).
"""
import os
import sys
from pathlib import Path

import duckdb
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
DATA_DIR = (ROOT / os.getenv("DATA_DIR", "../dataset/HHGOA_IEEE")).resolve()
STORE = ROOT / "data" / "store" / "graphsleuth.duckdb"


def build(force: bool = False) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    if STORE.exists() and not force:
        print(f"{STORE} exists; use --force to rebuild")
        return
    if STORE.exists():
        STORE.unlink()
    con = duckdb.connect(str(STORE))
    con.execute("PRAGMA memory_limit='6GB'")
    q = lambda name: f"read_csv_auto('{DATA_DIR / name}', header=true, sample_size=-1)"

    con.execute(f"CREATE TABLE txn AS SELECT * FROM {q('transactions.csv')}")
    con.execute(f"CREATE TABLE ident AS SELECT * FROM {q('identity.csv')}")
    con.execute(f"CREATE TABLE closed_cases AS SELECT * FROM {q('closed_cases_history.csv')}")
    con.execute(f"CREATE TABLE case_pack AS SELECT * FROM {q('case_pack.csv')}")

    # card id used throughout the README: <customer>-K<n>. Derive card key = card1 (disguised) per customer.
    con.execute("CREATE INDEX idx_txn_id ON txn(TransactionID)")
    con.execute("CREATE INDEX idx_ident_id ON ident(TransactionID)")

    # card_id = <customer>-K<rank over (card4, card6), NULLs first>. Verified 14,975/14,975
    # against every card id in closed_cases + case_pack.
    con.execute("""
        CREATE TABLE cards AS
        SELECT customer_id, card4, card6,
               customer_id || '-K' || row_number() OVER (
                   PARTITION BY customer_id
                   ORDER BY card4 ASC NULLS FIRST, card6 ASC NULLS FIRST) AS card_id
        FROM (SELECT DISTINCT customer_id, card4, card6 FROM txn)
    """)
    # Analysis view: one row per transaction with card_id, identity fields and the
    # README's device-profile string (DeviceInfo | OS | browser | screen).
    con.execute("""
        CREATE VIEW tx AS
        SELECT t.*, c.card_id,
               i.DeviceType, i.DeviceInfo, i.id_15, i.id_23, i.id_30, i.id_31, i.id_33, i.id_34,
               CASE WHEN i.TransactionID IS NULL THEN NULL ELSE
                    coalesce(i.DeviceInfo,'?') || ' | ' || coalesce(i.id_30,'?') || ' | ' ||
                    coalesce(i.id_31,'?') || ' | ' || coalesce(i.id_33,'?') END AS device_profile
        FROM txn t
        JOIN cards c ON c.customer_id = t.customer_id
                    AND c.card4 IS NOT DISTINCT FROM t.card4
                    AND c.card6 IS NOT DISTINCT FROM t.card6
        LEFT JOIN ident i ON i.TransactionID = t.TransactionID
    """)

    for t in ("txn", "ident", "closed_cases", "case_pack", "cards"):
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"{t}: {n:,} rows")
    con.close()


if __name__ == "__main__":
    build(force="--force" in sys.argv)
