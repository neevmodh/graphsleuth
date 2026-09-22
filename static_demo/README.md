# GraphSleuth — read-only demo

A static, frozen snapshot of the real app for sharing without exposing TigerGraph/LLM credentials (the live app has
no login, so those can never go on a public host). No backend, no build step, no API keys here at all — every file
is either static markup or a JSON snapshot recorded from a real run.

## Why this exists, not the live app

`api/main.py` needs a persistent TigerGraph MCP subprocess, a background thread per live investigation (SSE), and
in-memory/local-file state (`_investigations`, `data/store/approvals.json`) that resets on every cold start. None of
that survives Vercel's serverless model, and pushing TG_SECRET/GEMINI_API_KEY/GROQ_API_KEY to a host with no
authentication would be a real exposure. This demo sidesteps all of it: everything is pre-computed once, locally,
with real credentials, and only the *results* are published.

## Regenerating the snapshot

```bash
GRAPHSLEUTH_BACKEND=tigergraph .venv/bin/python scripts/freeze_static.py
```

Runs all 20 benchmark cases against the live TigerGraph + Groq/Gemini + GraphRAG backend (same as `run_cases.py`),
capturing the step-by-step tool-call log, the evidence graph, the transaction timeline and the counterfactuals panel
alongside the already-committed, hand-reviewed `cases/*.json` (which this script only reads — the answer text here
is always identical to the graded submission, never regenerated). Writes `static_demo/data/*.json`, one file per
case plus `index.json` (the queue) and `monitor.json` (the autonomous monitor's `cases_extra/*.json`, verbatim).
`static_demo/data/` IS committed (deliberately, unlike every other derived store in this repo) because Railway's
GitHub-integration deploy needs the files present in the repo it builds from. Regenerate with the command above,
`git add static_demo/data/`, and push — Railway redeploys on push to `main`.

## Deploying

Deployed on Railway as its own service (`static_demo/Dockerfile`, a bare `python -m http.server`, root directory
`static_demo/`), auto-deployed from GitHub on push to `main`. Pure static files, zero configuration, **no
environment variables set on this service at all** — the live app's TigerGraph/LLM credentials never go anywhere
near this host, which is the entire reason this demo exists as a separate deployment.

Can equally be deployed anywhere that serves static files (Vercel, Netlify, GitHub Pages, S3): `vercel deploy
static_demo --prod --yes` works unchanged if you'd rather not commit `data/` — just revert the `.gitignore` line
above and re-add the entry.

## What's different from the live app

- "Investigate" is "Replay investigation": it re-plays the *actual recorded* step list client-side (no network
  call), rather than running a new investigation.
- Approve/Reject is local, in-page state only — it is never sent anywhere and resets on reload. The live app
  persists it to `data/store/approvals.json` via a real endpoint.
- The theme toggle, search, filters, evidence graph and everything else work identically; they're already
  client-side in the live app too.
