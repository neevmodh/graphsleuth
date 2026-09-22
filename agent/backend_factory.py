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


def make_rag(backend, router):
    """GraphRAG over TigerGraph vectors (through the MCP server). None on the local backend, without keys, or if GRAPHSLEUTH_RAG=0."""
    if os.getenv("GRAPHSLEUTH_RAG", "1") == "0" or router is None or not router.available or not hasattr(backend, "conn"):
        return None
    try:
        from .graphrag import GraphRAG
        from .mcp_conn import MCPConnection
        return GraphRAG(router, MCPConnection(), conn=backend.conn)
    except Exception as e:                                  # noqa: BLE001
        print("GraphRAG unavailable:", type(e).__name__, str(e)[:100])
        return None
