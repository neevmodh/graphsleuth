# Blog outline: technical post for the TigerGraph submission

**Working title:** *Teaching a fraud agent to say "I'm not sure": graph-based investigation on TigerGraph*
**Alternatives:** *Fraud rings hide between risk scores* · *What a high risk score gets wrong*
**Length:** ~1,800 to 2,400 words, 4 figures, 2 short code blocks.
**Required by the task:** what we built, architecture, how TigerGraph is used, agentic capabilities, what we learned, what we would improve.

> **Publishing gate — cleared 2026-09-22.** The system now runs end to end on TigerGraph Savanna: 16 GSQL queries
> installed, GraphRAG live over TigerVector (through the TigerGraph MCP server, verified against 69 live tools),
> connected-component ring discovery and HITS hub ranking as graph-algorithm tools, and every case written back as a
> `FraudCase` vertex. Section 4 below can now be filled and cited directly from `README.md` and `docs/architecture.svg`
> instead of a plan. Still to confirm before publishing: the actual numbers (query count, timings) against whatever
> is true the day this goes out, since the graph and code keep moving.

## 0. TL;DR (3 sentences)
An agent that investigates card-fraud alerts, decides *how sure it is*, and recommends actions inside policy and
approval limits. Its best find was a fraud ring with no risky-looking transaction (risk score 0.05), visible only by
linking cards through a shared device. On held-out cases it reaches over 90% verdict accuracy on decided cases and a median
exposure error of $0, and it refuses to guess when the evidence is ambiguous.

## 1. The problem (150 words)
- Analysts trace money, link accounts, read policy, document, decide; slow and fragmented.
- A risk score is a reason to look, not a verdict: in the data every cleared false alarm scored above 0.7, and some fraud scored near zero.
- Fig. 1: two histograms of bank risk score (confirmed fraud vs cleared alerts). *Source: `closed_cases_history.csv` joined to `transactions.csv`.*

## 2. What we built (200 words)
- Investigates an alert from three triggers (risk score, customer report, analyst request), opens and progresses a case, recommends actions, files a report only when policy requires, and writes the case to memory.
- Fig. 2: screenshot of the analyst UI on HHG-014 (steps, gauge, actions with auto/L1/L2 chips).
- Design principle: **the LLM proposes; a deterministic policy engine disposes.** Action names, routes, the case-vs-report gate and rule R10 are code, not prompts.

## 3. Architecture (300 words)
- Fig. 3: architecture diagram (trigger, orchestrator, tool loop, graph, policy engine, memory, answer file, UI stream).
- Two-stage scoring: stage 1 scores each transaction in isolation; stage 2 judges it *in context* (neighbours, timing, shared device, region, product) and decides which transactions belong to one fraud episode.
- Policy engine: rules R1 to R10, exposure thresholds, stop rule, approval routing. 30 unit tests covering every rule and the verify-before-block boundary.
- Pattern detectors as graph/sequence queries: device ring, threshold structuring, card-testing sequence, recurring-charge test.

## 4. How TigerGraph is used  *(fill from the real implementation)*
- Schema: Customer, Card, Transaction, DeviceProfile, EmailDomain, BillingRegion, ClosedCase, Case (+ edges).
- Installed GSQL queries as agent tools, exposed through TigerGraph MCP; graph algorithms for ring discovery (connected components on the card-device graph).
- GraphRAG: closed-case narratives, policy sections and regulatory guidance in vector search, expanded through the graph (case, device, other cards) into a compact context pack.
- Case memory: every investigation written back as a `Case` vertex so the next analyst finds it.
- Fig. 4: the ring as drawn by the UI (one device, 27 connected cards) *and* the same neighbourhood in TigerGraph Insights, if used.
- Code block A: the device-burst query (short).

## 5. Agentic capabilities (250 words)
- Budgeted tool loop (7 to 8 calls per case) with every call streamed to the UI.
- Knows when to stop (p at or above 0.85 with two independent evidence items) and when it cannot decide (band 0.35 to 0.65, no invented customer reply, R4/R8 actions).
- Requests evidence through controlled, policy-approved actions; replies are simulated and *recorded as assumptions*.
- Initial vs final recommendation with `what_changed`; only `auto` actions can be executed; L1/L2 wait for a human.
- Explains itself: evidence with source and reference, rule cited on every action, standalone SAR narrative.
- Memory: cites the closed cases it used (e.g. the ring case cites CC-3035, CC-2985, CC-2971).

## 6. Results (150 words + one table)
- Table from `docs/eval_results.md` (held-out October cases). State the sample and that there is no answer key for the exam.
- Include the uncomfortable number: **16% to 32% false positives on legit look-alikes** across four samples (unflagged transactions with risk at or above 0.5).

## 7. What we learned (the heart of the post, ~500 words)
1. **The risk score is a trap.** Negatives must be matched to positives *within each risk band*, or the model learns "medium score means fraud".
2. **A silent sampling bug.** `USING SAMPLE` ran before `WHERE`, starving the negatives; the model looked great (AUC 0.98) and was overconfident. The honest retrain scored *lower* and was right. Lesson: check class balance, not just AUC.
3. **Fraud arrives in bursts.** Scoring transactions alone found 64% of fraud; scoring them in context lifted episode F1 from 0.52 to 0.88 (first held-out sample).
4. **Exposure decides the report.** The bank filed exactly when exposure exceeded $1,000 (393 of 393) or the pattern was undocumented (9 of 9). So episode accuracy, not just detection, drives the scored report decision.
5. **Raw device sharing is noise.** Common devices sit on hundreds of cards. The ring is rare *and* always New *and* always behind a proxy.
6. **Use exact rules where the labels are exact.** Channel mix and `New` device determine four of the five documented patterns perfectly; a classifier is needed only for in-person account takeover vs out-of-region use.
7. **A false "recurring charge".** Our first test called HHG-003 a subscription (gaps of 20 and 25 days); tightening to true monthly gaps removed it. Plausible-looking rules need a counter-check.
8. **The guard was right and my prompt was wrong.** The first real-model run had every rewrite rejected: I had asked for "concise", so the models dropped facts. Asking for a rewrite that keeps everything, plus one repair round naming the missing values, took summaries from 0 to 100% accepted.
9. **We stopped too early.** Our first ladder blocked at p >= 0.70, before the policy's own stop rule (p >= 0.85 with two independent evidence items). Moving the boundary halved wrongful blocks before verification (7% to 4% on identical samples) and made the recommendation visibly change after evidence, without touching accuracy.
10. **Not every "shared origin" in R6 generalizes.** The policy names three: device, billing region, recipient email. We measured before wiring region in: billing regions hold 90-580 active cards in a 7-day window, and 8-16 of them always score >=0.5 by the model's ordinary false-positive rate — there is no threshold that separates a real ring from a big city. Device profiles work as a ring signal because they are a rare, specific fingerprint; regions are a coarse geography shared by hundreds of unrelated customers. We measured this on all 20 exam cases before deciding, then implemented R6 for device-sharing only rather than ship a rule that would flag ordinary regional commerce as coordinated fraud.

## 8. Limitations, said plainly (150 words)
- The LLM fact guard checks numbers and IDs, not meaning: a real model turned an observation ("risk scores stay low") into a causal claim ("chosen to keep them low"). SAR rewrites were rejected outright because models drop card ids.
- Simulated customer replies: they follow the agent's own belief, so they cannot validate it.
- The ±72 h window uses transactions after the alert; a live system must use only what was known at alert time.
- The dev set has no ground truth for the exam distribution; numbers are a proxy.
- Two undocumented patterns were found by inspecting closed cases; a third may exist.
- R6's shared-origin detection covers device profiles only, not billing region or recipient email. Region sharing was tested and dropped for lack of signal (see "what we learned" #10). Recipient email (`R_emaildomain`) is not in the graph schema at all — it would need a schema change and a full reload, which we chose not to risk against the live workspace this close to the deadline.

## 9. What we would improve with more time (100 words)
- LLM-directed tool selection (the planner is rule-based today) with the policy engine unchanged.
- Time-honest evaluation (data up to alert time only).
- Ring discovery generalised with community detection over the card-device graph, run continuously as a monitor.
- Calibration per pattern; analyst feedback loop feeding the case memory.

## 10. Repo, demo, links
- GitHub repository, demo video, dataset credit (IEEE-CIS via Vesta, provided by TigerGraph for Hacker House Goa 2026), `@TigerGraphDB`.

## Fact-check list (verify before publishing)
- [ ] Every number is re-derived from `docs/eval_results.md` / `cases/*.json` at publish time.
- [ ] Figure 1 is produced from the data, not redrawn by hand.
- [ ] No sentence claims TigerGraph capability that the code does not use.
- [ ] Code blocks are copied from the repo, not retyped.
- [ ] The Kaggle IEEE-CIS files were never used (state this once; it is a disqualifying rule).
