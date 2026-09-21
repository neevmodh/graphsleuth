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

## Quick start
```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # add Gemini/Groq keys and Savanna details
.venv/bin/python data/load_local.py   # builds data/store/graphsleuth.duckdb from ../dataset/HHGOA_IEEE
.venv/bin/python -m pytest tests -q
```

The provided dataset is not included in this repository. Only the provided dataset is used; the public
Kaggle IEEE-CIS files are never used.
