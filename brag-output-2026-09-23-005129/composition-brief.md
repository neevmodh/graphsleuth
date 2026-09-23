# Hyperframes Composition Brief: GraphSleuth

## Objective
Create a short, cinematic, narrated launch-style brag video for GraphSleuth, an agentic fraud-investigation system built on TigerGraph.

## Output
- Composition directory: `brag-output-2026-09-23-005129/composition/`
- Rendered video: `brag-output-2026-09-23-005129/brag.mp4`
- Format: landscape — 1920x1080
- Duration: 24.9 seconds (voiceover-paced)

## Source Material
- Project root: `/Users/neev/Downloads/hackerHouseGoa/graphsleuth`
- Primary files read: `ui/index.html`, `docs/BLOG_OUTLINE.md`, `README.md`
- Product name: GraphSleuth
- Tagline / strongest claim: "Its best find was a fraud ring with no risky-looking transaction (risk score 0.05), visible only by linking cards through a shared device."
- Key UI moments recreated: case hero card (verdict chip + uncertainty gauge), investigation-steps list, evidence graph (device node + connected cards), next-best-action chips with route badges, SAR-filed banner.

## Creative Direction
- Tone preset: cinematic
- Angle: The bank's own risk model scored the flagged transaction 0.05 — as safe as it gets. GraphSleuth found a 28-card fraud ring hiding behind it anyway, by walking the graph instead of trusting the score.
- Avoid: generic SaaS language, abstract filler visuals, comedic/bouncy sound design.

## Visual Identity
- Background #0A0C0F, text #F3F4F1, accent #E8A33D, bad/fraud #FF5C70, ok #49C38A, uncertain #9B8CFF, doc #2DD4BF.
- System sans/mono fonts (no external @font-face dependency, avoids the font_family_without_font_face lint rule).

## Audio
- Voiceover: Kokoro TTS (`am_michael`), 5 lines, generated as separate WAV files per scene for precise sync — durations measured via ffprobe and used directly as clip/audio timings.
- No music bed: the only bundled tracks available were upbeat corporate ("happy-beats-business-moves"), tonally wrong for a fraud-thriller narration; layering them would compete with the voice, so SFX + voice only.
- SFX: bundled from media-use's sfx library — impact-bass-1 (hit), click-soft (tick), click (lock), error (alert), riser (tension bed under scenes 2-3).

## Build notes / bugs found and fixed during composition
1. `#s2-gauge-marker` originally animated via CSS `left` — lint error `gsap_non_transform_motion` (layout property snaps to integer pixels and stutters under frame-seek capture). Fixed by animating `x` (GSAP transform) with `xPercent/yPercent` set once for centering.
2. `#s4` (action-chip scene) and `#s3a` (steps scene) had no explicit height, so their flex `justify-content: center` centered within a collapsed auto-height box instead of the full 1080px frame — rows rendered near the top and the layout audit caught a real `content_overlap` between the SAR banner and the action rows. Fixed with `height: 100%`.
3. `#s3b` (the evidence-graph layer) has no `position` set beyond `opacity`, so once `#s3a` was fixed to `height: 100%` (a normal in-flow block filling the frame), `#s3b` as its next sibling was pushed into normal document flow *below* it — rendering the entire graph scene off-screen beneath the visible frame, confirmed via snapshot (only the very top of the device circle peeked in at the bottom edge). Fixed with `position: absolute; inset: 0`.

All three fixes were caught before rendering via `npx hyperframes check` (lint + layout audit) and `npx hyperframes snapshot` visual inspection, not left in the delivered video.

## Requirements checklist
- [x] Shows real UI/copy from the source project (dashboard, graph, action chips)
- [x] All text readable in the final render
- [x] Duration within target (24.9s, voice-paced)
- [x] Audio layer included (voiceover + SFX)
- [x] `npx hyperframes check` passes (0 errors)
