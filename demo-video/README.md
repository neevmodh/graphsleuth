# GraphSleuth — 3-minute submission demo video

`graphsleuth-demo.mp4` — 180s, 1920x1080, **light/colourful theme**, music bed, **no voiceover**
(a narration track is added separately over the top). Built with HyperFrames; source in `composition/`.

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

## Look
Light theme, built from the project's **own** light palette (`ui/index.html`'s `[data-theme="light"]`):
warm off-white `#F7F5EF`, amber `#C67A1E`, plus teal / violet / green / coral accents. A slow drifting
mesh-gradient backdrop (four blurred radial blobs on a 90s yoyo) runs under the whole piece, with a fine grain
overlay. The screen recordings are captured with the console switched to **light mode** so the footage matches
the graphics instead of fighting them.

Motion: per-character split-text reveals with 3D `rotateX`, an iris `clip-path` wipe on the hook, deterministic
pre-baked count-ups, 3D `rotateY`/`rotateX` card entrances on a shared perspective, an SVG `stroke-dashoffset`
path draw under the pipeline, `elastic.out` spring orbit for the ring nodes, Ken Burns punch-ins on every footage
segment, and glassmorphic annotation cards.

## Audio
A music bed (`assets/music/bed.mp3` — 180s, crossfade-looped from the bundled upbeat track, 4s fade-out)
sits low at `data-volume="0.34"`, with sparse SFX on top: impacts on the two hard cuts, ticks through the
action-chip sequence, one alert on the SAR banner, and a riser under the ring build. Measured on the final
render: mean −22 dB, peaks −1.5 dB — deliberately quiet so a voiceover drops straight on without re-mixing.

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
