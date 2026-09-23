# GraphSleuth — 3-minute submission demo video

`graphsleuth-demo.mp4` — 180s, 1920x1080, **no voiceover** (a narration track is added separately over the top).
Built with HyperFrames; source in `composition/`.

## Why this is separate from the teaser
`../brag-output-2026-09-23-005129/brag.mp4` is a ~25s narrated social teaser for the required social post.
This is the hackathon's required **3–5 minute demo video showing the agent working end to end**: longer, denser,
and built around real screen recordings rather than motion graphics alone.

## Structure

| Time | Section | What's on screen |
|---|---|---|
| 0:00–0:05 | Hook | `0.05` bank risk score → hard cut to "28 cards. One device. One ring." |
| 0:05–0:12 | Title | GraphSleuth wordmark, tagline, Hacker House Goa 2026 · Task 04 |
| 0:12–0:18 | The dataset | 590,742 transactions · 0 fraud labels · 20 benchmark cases |
| 0:18–0:32 | The problem | "A risk score is a reason to look. Never a verdict." |
| 0:32–0:52 | Pipeline | Trigger → Investigate (17 GSQL) → GraphRAG → Policy R1–R10 → Case memory, built one node at a time |
| 0:52–1:06 | TigerGraph stack | 17 GSQL queries · 2.3M edges · 69 MCP tools · TigerVector · ring discovery · HITS · FraudCase write-back |
| 1:06–1:18 | Design principle | "The LLM proposes / The policy engine disposes" |
| **1:18–2:06** | **SCREEN RECORDING — live investigation** | The real analyst console running HHG-014 against **live TigerGraph Savanna**: 12 tool calls streaming with real latencies, evidence graph, SAR narrative. Annotated with callouts. |
| 2:06–2:26 | The find | Animated ring reveal: 28 cards around one device |
| 2:26–2:40 | Policy engine | The five recommended actions locking in with their `auto`/`L1`/`L2` routes, then the SAR banner |
| **2:40–2:52** | **SCREEN RECORDING — the repo** | github.com/neevmodh/graphsleuth: README, file tree, the 20 answer files |
| 2:52–2:56 | Results | 20/20 valid · 0 backend differences · 84 tests · 3 undocumented patterns |
| 2:56–3:00 | Outro | Links, Hacker House Goa 2026, @TigerGraphDB |

## Audio
Sparse SFX only (impacts on the two hard cuts, ticks on the action-chip sequence, one alert on the SAR banner,
a low riser under the ring build). Deliberately no music bed and no narration — both are left free so a voiceover
can sit cleanly on top without ducking or re-mixing.

## Screen recordings
Captured with Playwright (`scripts/record_demo.js`) at 1920x1080:
- `composition/assets/footage/ui.mp4` — 86s of the real analyst console, served by `api/main.py` with
  `GRAPHSLEUTH_BACKEND=tigergraph`. The "backend: TigerGraph" badge is visible in frame; the investigation is a
  genuine live run, not a replay.
- `composition/assets/footage/github.mp4` — 31s of the public repository.

Re-record with:
```bash
GRAPHSLEUTH_BACKEND=tigergraph .venv/bin/python -m uvicorn api.main:app --port 8000   # in one shell
node scripts/record_demo.js ui        # in another
node scripts/record_demo.js github
```
Note: recording the UI runs a real investigation, and the `/run` endpoint writes its result to `cases/<id>.json` —
verify that file afterwards (`python -m eval.validate_answers cases`) before committing.

## Rebuild
```bash
cd composition
npx hyperframes check                                   # 0 errors expected
npx hyperframes render --quality looks --output ../graphsleuth-demo.mp4
```
