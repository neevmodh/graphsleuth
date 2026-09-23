# Brag Plan: GraphSleuth

## What is this app?
An AI agent that investigates card-fraud alerts on TigerGraph, decides how sure it is, and recommends bank actions inside policy and approval limits — built for TigerGraph's Hacker House Goa 2026 hackathon.

## The angle
The bank's own risk model scored the flagged transaction 0.05 — as safe as it gets. GraphSleuth found a 28-card fraud ring hiding behind it anyway, by walking the graph instead of trusting the score. The video's whole premise: the number said "fine," the graph said "ring."

## Hook (first 2-3 seconds)
Giant "0.05" on near-black, small caption "risk score" beneath it — reads as reassuring, almost boring. Hard flash-cut to "28 CARDS. ONE DEVICE. ONE RING." in red.

## Key moments (the middle)
- The real analyst dashboard: verdict chip flips to FRAUD, the uncertainty gauge needle sweeps to the fraud end, pattern reads "undocumented."
- Investigation steps streaming in one by one (real tool calls: get_transaction, device_neighbors, device_burst, ring_component) ending on "28 cards, 1 device."
- The evidence graph exploding outward from the flagged card to 27 connected cards around one shared device node.
- Policy actions locking in one by one with their approval-route chips: BLOCK_CARD (L1) → CREATE_CASE (auto) → FILE_REPORT (L2) → ESCALATE_TO_ANALYST (auto), then the SAR-filed banner.

## Outro / punchline
"An agentic fraud investigator on TigerGraph." Repo + live demo links. Built for Hacker House Goa 2026 · tag @TigerGraphDB.

## User flow worth showing
Entry: an analyst-request alert comes in on a transaction with a near-zero risk score. Key action: the agent walks the graph (device → 27 other cards, GraphRAG grounds the recommendation in policy R6/R9), evidence and uncertainty gauge update live. Result: BLOCK_CARD + FILE_REPORT + ESCALATE_TO_ANALYST, case written to the graph, ring exposed and named.

## Tone
- Preset: cinematic
- Creative direction: a fraud-investigator agent uncovering a hidden ring the risk score missed — security-thriller energy, not corporate demo energy.
- Interpretation: wide dramatic holds, big type for the hook and reveal numbers, restrained but weighty motion; let the real UI carry the middle instead of marketing copy.

## Format: landscape — 1920x1080
## Duration: 24.9 seconds (voiceover-paced)

## Visual identity (from the project)
- Background: #0A0C0F (near-black)
- Accent: #E8A33D (amber)
- Text: #F3F4F1 (off-white)
- Bad/fraud: #FF5C70 · Ok/legitimate: #49C38A · Uncertain: #9B8CFF · Doc/evidence: #2DD4BF
- Display font: system sans (no @font-face dependency)
- Body/mono font: system monospace
- Strongest visual element: the uncertainty gauge and the evidence graph (device node at center of a card ring)

## Share copy (draft)
Risk score: 0.05. Verdict: fraud. GraphSleuth found a 28-card ring hiding behind one "safe" transaction — an agentic fraud investigator built on TigerGraph for Hacker House Goa 2026.

## Audio direction
- Role: narrated + SFX, no music bed (available bundled tracks were upbeat corporate, mismatched to a fraud-thriller narration; layering them would compete with the voice)
- Voiceover: Kokoro TTS, voice am_michael, 5 lines timed to each scene
- SFX posture: moderate, motion-matched — hard hit on the flash-cut, ticks on step reveals, thunks on graph-node arrivals, chip-lock sound per action, alert cue on the SAR banner

## Voiceover script
1. "Risk score: zero point zero five. Safe, by the numbers." (4.22s)
2. "But GraphSleuth doesn't trust the number, it walks the graph." (3.43s)
3. "One device. Twenty-eight cards. A coordinated ring, hiding in plain sight." (5.40s)
4. "The policy engine locks it down: block the card, file the report, escalate to a human." (6.02s)
5. "GraphSleuth. An agentic fraud investigator, built on TigerGraph." (4.76s)

## Storyboard
### Scene 1 — The number that lied — 0-4.4s
"0.05" / "risk score" hold, flash-cut to "28 CARDS. ONE DEVICE. ONE RING." VO line 1.
### Scene 2 — The verdict flips — 4.4-8.1s
Hero card, gauge sweeps and locks, verdict flips to FRAUD, pattern "undocumented". VO line 2.
### Scene 3 — Walking the graph — 8.1-13.7s
5 investigation steps stream in, then the evidence graph explodes to 28 cards. VO line 3.
### Scene 4 — The bank moves — 13.7-19.9s
4 action chips lock in with route badges, SAR-filed banner. VO line 4.
### Scene 5 — Outro — 19.9-24.9s
GraphSleuth wordmark, tagline, repo + demo links, Hacker House Goa 2026, @TigerGraphDB. VO line 5.
