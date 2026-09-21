"""TigerGraph connection through the TigerGraph MCP server.

The agent's graph calls go over the Model Context Protocol to the `tigergraph-mcp` server (github.com/tigergraph/tigergraph-mcp),
which talks to TigerGraph. This class holds one persistent stdio session (started lazily, on a background event loop so the
rest of the agent stays synchronous) and exposes the subset of the pyTigerGraph API that `TigerGraphBackend` and
`TigerGraphCaseMemory` use, so switching between direct REST and MCP is a one-line change (GRAPHSLEUTH_TG_VIA=mcp).

The server runs in its own virtualenv (.venv-mcp) because it needs pyTigerGraph 2.x while the loader uses 1.x.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


class MCPError(RuntimeError):
    pass


def _server_params():
    from mcp import StdioServerParameters
    from mcp.client.stdio import get_default_environment
    cfg = {**dotenv_values(ROOT / ".env"), **{k: v for k, v in os.environ.items() if k.startswith("TG_")}}
    env = {**get_default_environment(), "TG_HOST": cfg["TG_HOST"], "TG_GRAPHNAME": cfg.get("TG_GRAPH", "GraphSleuth"),
           "TG_TGCLOUD": "true" if "tgcloud.io" in cfg["TG_HOST"] else "false"}
    for k in ("TG_SECRET", "TG_USERNAME", "TG_PASSWORD"):      # stdio_client forwards only what we pass explicitly
        if cfg.get(k):
            env[k] = cfg[k]
    cmd = os.getenv("TIGERGRAPH_MCP_CMD", str(ROOT / ".venv-mcp" / "bin" / "tigergraph-mcp"))
    return StdioServerParameters(command=cmd, args=[], env=env)


class MCPConnection:
    """pyTigerGraph-shaped facade over MCP tool calls."""

    def __init__(self, graph: str | None = None):
        self.graph = graph or os.getenv("TG_GRAPH", "GraphSleuth")
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._session = None
        self._stack = None
        self.calls = 0
        self._run(self._open())

    # ---- session plumbing -----------------------------------------------------------------------------------
    def _run(self, coro, timeout: float = 180):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _open(self):
        from contextlib import AsyncExitStack
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client
        self._stack = AsyncExitStack()
        r, w = await self._stack.enter_async_context(stdio_client(_server_params()))
        self._session = await self._stack.enter_async_context(ClientSession(r, w))
        await self._session.initialize()

    def close(self):
        try:
            self._run(self._stack.aclose(), 20)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def tools(self) -> list[str]:
        return [t.name for t in self._run(self._session.list_tools()).tools]

    def call(self, tool: str, **args) -> dict:
        """Call `tigergraph__<tool>` and return the parsed JSON envelope's `data`."""
        args.setdefault("graph_name", self.graph)
        res = self._run(self._session.call_tool(f"tigergraph__{tool}", arguments=args))
        self.calls += 1
        text = "".join(getattr(c, "text", "") for c in res.content).strip()
        try:
            env = json.loads(_FENCE.sub("", text))
        except ValueError as e:
            raise MCPError(f"{tool}: unparseable response: {text[:200]}") from e
        if not env.get("success", False):
            raise MCPError(f"{tool}: {env.get('error') or env.get('message') or env.get('summary')}")
        return env.get("data", env)

    # ---- the pyTigerGraph subset the backend uses ---------------------------------------------------------------
    def runInstalledQuery(self, name, params=None, timeout=None, sizeLimit=None):
        data = self.call("run_installed_query", query_name=name, params=_encode_params(params or {}))
        return data["results"] if isinstance(data, dict) and "results" in data else data

    def getVerticesById(self, vtype, vid):
        data = self.call("get_node", vertex_type=vtype, vertex_id=vid)
        node = data.get("node") or data.get("vertex") or data
        attrs = node.get("attributes", node) if isinstance(node, dict) else {}
        return [{"attributes": attrs}] if attrs else []

    def upsertVertex(self, vtype, vid, attributes):
        return self.call("add_node", vertex_type=vtype, vertex_id=vid, attributes=attributes)

    def upsertEdge(self, src_t, src_id, etype, dst_t, dst_id, attributes=None):
        return self.call("add_edge", source_vertex_type=src_t, source_vertex_id=src_id, edge_type=etype,
                         target_vertex_type=dst_t, target_vertex_id=dst_id, attributes=attributes or {})

    def getEdges(self, vtype, vid, etype=None):
        data = self.call("get_node_edges", vertex_type=vtype, vertex_id=vid)
        edges = data.get("edges", data) if isinstance(data, dict) else data
        return [e for e in edges if not etype or e.get("e_type") == etype] if isinstance(edges, list) else []


def _encode_params(params: dict) -> dict:
    """pyTigerGraph-style vertex params are (id, type) tuples; over JSON they become {"id": ..., "type": ...}."""
    out = {}
    for k, v in params.items():
        out[k] = {"id": v[0], "type": v[1]} if isinstance(v, tuple) and len(v) == 2 else v
    return out
