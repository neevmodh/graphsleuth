# GraphSleuth

**An agentic fraud investigator that thinks in graphs.**
Built on [TigerGraph](https://www.tigergraph.com) for Hacker House Goa 2026, Task 04.

GraphSleuth investigates a fraud alert (risk score, customer report, or analyst request), gathers evidence from a
TigerGraph knowledge graph and GraphRAG over policy and past cases, assesses risk under uncertainty, opens and
progresses a case, recommends next-best actions within policy and approval limits, and writes what it learned
back to the graph as case memory.

> Status: runs end to end on **TigerGraph Savanna**. The graph (590,742 transactions, 14,318 cards, 5,565 closed cases,
> ~2.3M edges, counts verified against the source) is loaded, 13 GSQL queries are installed, and all 20 benchmark cases run
> through them and are written back to the graph as `FraudCase` vertices. The TigerGraph answers are identical to the local
> backend's (0 differences over 100 field groups). Pending: GraphRAG (needs a Gemini key), routing calls through the MCP
> server end to end, a real-provider run of the LLM layer. Held-out results: [docs/eval_results.md](docs/eval_results.md).

## Design principle
**The LLM proposes; a deterministic policy engine disposes.** Fraud probability comes from graph features and a
calibrated scorer; action names, approval routes (`auto` / `L1` / `L2`), the case-vs-report gate and the stop rule
are enforced in code (`agent/policy.py`), so policy is never hallucinated.

## Layout
| Path | Purpose |
|---|---|
| `data/` | Local DuckDB feature store and (later) TigerGraph loaders |
| `graph/` | GSQL schema and installed queries |
| `agent/` | Orchestrator, tools, scorer, policy engine, memory, SAR writer, answer schema |
| `eval/` | Dev-set replay on closed cases and answer validation |
| `api/`, `ui/` | FastAPI + SSE backend and React investigator UI |
| `cases/` | The 20 benchmark answer files |
| `docs/` | Data findings, architecture, blog |

## Run the analyst UI
```bash
.venv/bin/python run_cases.py                 # writes cases/HHG-001..020.json (validated: python -m eval.validate_answers cases)
.venv/bin/python -m uvicorn api.main:app --port 8000   # then open http://localhost:8000
```
Pick a case and press **Investigate** to watch each tool call stream in, then read the evidence, the initial vs final
actions with their approval routes (L1/L2 actions wait for a human: Approve / Reject), the evidence graph, the
transaction timeline and the suspicious activity report.

## TigerGraph backend
The agent talks to the graph through one interface (`agent/backend.py`). `GRAPHSLEUTH_BACKEND=tigergraph` switches it to
`agent/tg_backend.py`, which calls the installed GSQL queries in `graph/queries.gsql`. Setup on a Savanna workspace
(auto-suspend and auto-resume ON):
```bash
cp .env.example .env                           # TG_HOST + TG_SECRET (a Database Secret; no password needed)
python -m data.export_tg                       # load-ready CSVs in data/store/tg/
python - <<'PY'                                # local-schema graph, so it never touches other graphs in the workspace
from pyTigerGraph import TigerGraphConnection; import os
from dotenv import load_dotenv; load_dotenv()
c = TigerGraphConnection(host=os.environ["TG_HOST"], graphname="", gsqlSecret=os.environ["TG_SECRET"], tgCloud=True); c.getToken(os.environ["TG_SECRET"])
print(c.gsql(open("graph/schema.gsql").read()))
PY
python -m data.load_tg                         # REST loader, idempotent (~25 min for the full graph)
python graph/install_queries.py                # creates and installs the 13 queries (~4 min)
GRAPHSLEUTH_BACKEND=tigergraph python run_cases.py && pytest tests/test_tg_live.py
```
The TigerGraph MCP server (`tigergraph-mcp`) connects to the workspace over stdio (verified); `agent/mcp_conn.py` is the
adapter that routes the agent's graph calls through it, and is not yet verified end to end.

## LLM layer
`agent/llm.py` routes to Groq (fast tool loop) and Gemini (synthesis, embeddings) through their OpenAI-compatible endpoints,
with retry/backoff, provider fallback, an on-disk cache and token accounting. `agent/explain.py` lets the model reword the
case summary and the SAR narrative, but a fact guard rejects any rewrite that drops or invents a number, amount, date or ID
(the template text is kept instead). With no keys everything runs offline on the templates. Set `GROQ_API_KEY` and
`GEMINI_API_KEY` in `.env` to enable it; it has been tested with fake clients only, not against the real providers.

## Quick start
```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # add Gemini/Groq keys and Savanna details
.venv/bin/python data/load_local.py   # builds data/store/graphsleuth.duckdb from ../dataset/HHGOA_IEEE
.venv/bin/python -m pytest tests -q
```

The provided dataset is not included in this repository. Only the provided dataset is used; the public
Kaggle IEEE-CIS files are never used.
