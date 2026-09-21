"""Pick the graph backend: GRAPHSLEUTH_BACKEND=tigergraph (needs TG_* settings) or the default local DuckDB store."""
from __future__ import annotations

import os

from dotenv import load_dotenv

from .scorer import ROOT

load_dotenv(ROOT / ".env")


def make_backend():
    """Return (backend, memory, name)."""
    if os.getenv("GRAPHSLEUTH_BACKEND", "local").lower() == "tigergraph":
        from .tg_backend import TigerGraphBackend, TigerGraphCaseMemory, connect
        conn = connect()
        return TigerGraphBackend(conn), TigerGraphCaseMemory(conn), "tigergraph"
    from .backend import LocalBackend
    from .memory import LocalCaseMemory
    return LocalBackend("final"), LocalCaseMemory(), "local-duckdb"
