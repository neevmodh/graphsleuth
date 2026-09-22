# GraphSleuth

**An agentic fraud investigator that thinks in graphs.**
Built on [TigerGraph](https://www.tigergraph.com) for Hacker House Goa 2026, Task 04.

GraphSleuth investigates a fraud alert (risk score, customer report, or analyst request), gathers evidence from a
TigerGraph knowledge graph and GraphRAG over policy and past cases, assesses risk under uncertainty, opens and
progresses a case, recommends next-best actions within policy and approval limits, and writes what it learned
back to the graph as case memory.

![Architecture](docs/architecture.svg)

> Status: runs end to end on **TigerGraph Savanna**. The graph (590,742 transactions, 14,318 cards, 5,565 closed cases,
> ~2.3M edges, counts verified against the source) is loaded, 16 GSQL queries are installed, and all 20 benchmark cases run
> through them and are written back to the graph as `FraudCase` vertices. The TigerGraph answers are identical to the local
> backend's (0 differences over 100 field groups). GraphRAG is live (policy/typology/regulatory chunks retrieved via
> TigerVector over the TigerGraph MCP server, grounding evidence, the case summary and the SAR narrative), agent calls
> route through the MCP server end to end (verified against 69 live tools), and the LLM layer runs on real Groq + Gemini
> keys. Graph algorithms (connected-component ring discovery, HITS hub ranking, card-to-card link paths) and counterfactual
> explanations with an uncertainty read-out are wired into the investigator and the UI. Held-out results:
> [docs/eval_results.md](docs/eval_results.md).

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
| `docs/` | Data findings, [architecture diagram](docs/architecture.svg), demo script, blog outline |

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
python graph/install_queries.py                # creates and installs the 16 queries (~5 min)
GRAPHSLEUTH_BACKEND=tigergraph python run_cases.py && pytest tests/test_tg_live.py
```
The TigerGraph MCP server (`tigergraph-mcp`) connects to the workspace over stdio (verified: 69 tools listed);
`agent/mcp_conn.py` is the adapter that routes the agent's GraphRAG retrieval through it end to end, verified live.

### Graph algorithms (`agent/tg_backend.py`)
`ring_component` (connected-component ring discovery: hops only through devices that are rare AND mostly-New AND
mostly-proxied, so an ordinary shared device never floods the traversal), `device_hub_rank` (HITS power iteration over
the card-device bipartite graph, surfacing the device at the centre of a ring, not just the one with the most cards) and
`card_link` (hop distance between two specific cards). The investigator calls `ring_component` whenever its existing
single-device ring heuristic fires, to check whether the ring reaches beyond that one device.

### Counterfactuals and uncertainty (`agent/counterfactual.py`)
For a case that fired the ring/testing/structuring/customer-report/recurring adjustments, `GET
/api/cases/{id}/counterfactuals` replays the same deterministic probability formula with one signal flipped off,
reporting whether that would cross a policy threshold — plus how far the case sits from the nearest one. Shown in the UI
under the fraud-probability gauge after each live investigation.

### Autonomous monitor (`run_monitor.py`, optional)
Sweeps the whole graph for alerts the 20 sampled benchmark cases never triggered on — every ring-like device
`find_rings()` finds (not just HHG-014's) and the highest bank-risk-score transactions that were never sampled — and
investigates them the same way `run_cases.py` does. Output goes to `cases_extra/` and is never written back to the
graph, kept separate from the 20 graded answers.

## LLM layer
`agent/llm.py` routes to Groq (`openai/gpt-oss-120b`, fast tool loop) and Gemini (`gemini-3.6-flash`, synthesis; embeddings) through
their OpenAI-compatible endpoints, with retry/backoff, key rotation, provider fallback, an on-disk cache and token accounting.
`agent/explain.py` lets the model reword the case summary and SAR narrative behind a **fact guard**: every number, amount, date and ID
must survive, otherwise the template text is kept, and a failed rewrite gets one repair round naming the missing values.
Measured with the real providers: 16 of 20 summaries were reworded and accepted; the SAR rewrites were rejected (the models drop card
ids), so SARs stay on the checked template. Known limit: the guard checks facts, not meaning, so a model can still add a mild
inference (e.g. turning "risk scores stay low" into "chosen to keep risk scores low"). `run_cases.py --no-llm` reproduces the
template-only answers. Keys: `GROQ_API_KEY` / `GEMINI_API_KEY` in `.env`, comma-separated to rotate several.

## Quick start
```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # add Gemini/Groq keys and Savanna details
.venv/bin/python data/load_local.py   # builds data/store/graphsleuth.duckdb from ../dataset/HHGOA_IEEE
.venv/bin/python -m pytest tests -q
```

The provided dataset is not included in this repository. Only the provided dataset is used; the public
Kaggle IEEE-CIS files are never used.
