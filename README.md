# GraphSleuth

**An agentic fraud investigator that thinks in graphs.**
Built on [TigerGraph](https://www.tigergraph.com) for Hacker House Goa 2026, Task 04.

GraphSleuth investigates a fraud alert (risk score, customer report, or analyst request), gathers evidence from a
TigerGraph knowledge graph and GraphRAG over policy and past cases, assesses risk under uncertainty, opens and
progresses a case, recommends next-best actions within policy and approval limits, and writes what it learned
back to the graph as case memory.

> Status: work in progress. See the [issues](../../issues) and milestones for the phased plan.
> Working end to end on a local DuckDB backend: all 20 benchmark cases produce validated answer files (`cases/`).
> Pending: TigerGraph Savanna backend (GSQL queries, GraphRAG, case write-back), LLM layer, UI. Dev results: [docs/eval_results.md](docs/eval_results.md).

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
`agent/tg_backend.py`, which calls the installed GSQL queries in `graph/queries.gsql`:
```bash
python -m data.export_tg                       # writes load-ready CSVs to data/store/tg/
# on the Savanna workspace: schema, load, queries
gsql graph/schema.gsql && gsql -g GraphSleuth graph/load.gsql && gsql -g GraphSleuth graph/queries.gsql
gsql -g GraphSleuth "INSTALL QUERY ALL"
GRAPHSLEUTH_BACKEND=tigergraph python run_cases.py        # then: pytest tests/test_tg_live.py
```
Status: the schema and all 13 queries were type-checked against a real TigerGraph 4.2.5. The backend class is unit-tested
against a fake connection only; it has not yet run against a live instance (`tests/test_tg_live.py` will verify it).

## Quick start
```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # add Gemini/Groq keys and Savanna details
.venv/bin/python data/load_local.py   # builds data/store/graphsleuth.duckdb from ../dataset/HHGOA_IEEE
.venv/bin/python -m pytest tests -q
```

The provided dataset is not included in this repository. Only the provided dataset is used; the public
Kaggle IEEE-CIS files are never used.
