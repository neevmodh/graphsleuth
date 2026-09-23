# Teaching a fraud agent to say "I'm not sure"

**Graph-based fraud investigation on TigerGraph — built for Hacker House Goa 2026, Task 04.**

The best thing our agent found was a transaction the bank's own model scored **0.05**. As safe as a risk score gets.
Behind it sat a coordinated fraud ring across **28 cards**, sharing one rare Android device behind an anonymous proxy.
Nothing about that transaction looked alarming on its own. It was only visible by walking the graph.

That single case is the argument for this whole project: a risk score is a reason to look, never a verdict.

- **Repo:** [github.com/neevmodh/graphsleuth](https://github.com/neevmodh/graphsleuth)
- **Live read-only demo:** [graphsleuth-production.up.railway.app](https://graphsleuth-production.up.railway.app)

---

## 1. The problem

A fraud analyst investigating one alert has to pull the card's history, trace the money, find connected accounts,
check the device, read the policy, weigh how sure they are, document all of it, and only then decide. It's slow,
fragmented, and often finishes after the money is gone.

The obvious shortcut — trust the model score — doesn't survive contact with the data. In the dataset we were given
(590,742 card transactions over six months, with the fraud label **removed** and replaced by the bank's own risk
score), the score is wrong in both directions: cleared false alarms routinely score above 0.7, and real confirmed
fraud scores near zero. Sorting by risk score gets you a queue, not an answer.

## 2. What we built

**GraphSleuth** takes one alert — from a risk score, a customer report, or an analyst request — and runs a full
investigation: gathers evidence from a TigerGraph knowledge graph, retrieves policy and past cases through GraphRAG,
decides how confident it is, opens and progresses a case, recommends next-best actions inside policy and approval
limits, files a suspicious activity report only when policy demands one, and writes what it learned back to the graph
so the next investigation can find it.

The design principle that shaped everything:

> **The LLM proposes. A deterministic policy engine disposes.**

Fraud probability comes from graph features and a calibrated scorer. Action names, approval routes (`auto` / `L1` /
`L2`), the case-versus-report gate, rules R1–R10 and the stop rule all live in code (`agent/policy.py`, 30 unit
tests). The model never invents a policy decision — it reasons over evidence and writes prose. That split is why the
agent's output is auditable.

## 3. Architecture

An alert enters the orchestrator, which runs a budgeted tool loop against the graph (8–12 calls per case), assembles
an `Assessment`, and hands it to the policy engine. Everything streams to the analyst UI as it happens.

Two pieces are worth calling out.

**Two-stage scoring.** Stage one scores each transaction in isolation. Stage two judges it *in context* — neighbouring
transactions on the card, timing, shared device, region, product — and decides which transactions belong to the same
fraud episode. This mattered enormously (see lesson 2 below).

**Pattern detection as graph and sequence queries, not prompts.** The device ring, threshold structuring, the
card-testing sequence and the recurring-charge test are all deterministic detectors over graph results. An LLM can
describe a ring; it shouldn't be the thing that decides one exists.

## 4. How TigerGraph is used

The graph holds Customer, Card, Transaction, DeviceProfile, EmailDomain, BillingRegion, ClosedCase and FraudCase
vertices — 22 vertex and edge types, about 2.3M edges over the full dataset, loaded into TigerGraph Savanna.

**Installed GSQL queries as agent tools.** Every tool the agent can call is one of **17 installed GSQL queries**. The
agent doesn't generate GSQL at runtime; it calls a fixed, reviewed toolkit. Here's the one that finds a device's
neighbourhood — the query behind the ring discovery:

```sql
CREATE OR REPLACE QUERY device_neighbors(VERTEX<DeviceProfile> dev, DATETIME t_from, DATETIME t_to)
FOR GRAPH GraphSleuth SYNTAX v3 {
  MapAccum<STRING, SumAccum<INT>> @@n;
  MapAccum<STRING, SumAccum<DOUBLE>> @@total;
  Start = {dev};
  T = SELECT t FROM Start:d -(DEVICE_OF>)- Transaction:t WHERE t.ts >= t_from AND t.ts <= t_to
      ACCUM @@n += (t.card_id -> 1), @@total += (t.card_id -> t.amt);
  C = SELECT cc FROM Start:d -(DEVICE_OF>)- Transaction:t -(INVOLVED_IN>)- ClosedCase:cc;
  PRINT Start[Start.n_cards AS n_cards, Start.new_frac AS new_frac, Start.proxy_frac AS proxy_frac];
  PRINT @@n AS window_n, @@total AS window_total;
  PRINT C[C.id AS case_id, C.outcome AS outcome, C.pattern AS pattern];
}
```

**Graph algorithms.** `ring_component` is a connected-component traversal that only hops through devices that are
rare *and* mostly-New *and* mostly-proxied — so an ordinary shared family device never floods the traversal.
`device_hub_rank` is a HITS power iteration over the card-device bipartite graph, which surfaces the device at the
*centre* of a ring rather than the one with the most cards. On the ring case, `ring_component` reached 52 cards —
more than the 28 visible in the flagged burst window alone.

**GraphRAG over TigerVector.** Closed-case narratives, the bank's fraud policy sections, the documented typologies and
regulatory guidance (FinCEN SAR narrative standards, ATO advisories) are embedded and stored in TigerGraph's vector
search, then expanded through the graph into a compact context pack. The agent's evidence list cites what it
retrieved — `graphrag:POL-R9`, `graphrag:TYPO-0079` — so every grounded claim is traceable.

**TigerGraph MCP.** The whole backend can route through the TigerGraph MCP server (verified live against 69 exposed
tools). `GRAPHSLEUTH_TG_VIA=mcp` swaps the transport without changing a line of agent code.

**Case memory.** Every investigation is written back as a `FraudCase` vertex with edges to its transactions, device
and connected cards. The next alert's `similar_cases` query finds it. That's the loop closing.

## 5. Agentic capabilities

- **Budgeted tool loop**, 8–12 calls per case, every call streamed to the UI with its real latency.
- **Knows when to stop**: probability ≥ 0.85 (or ≤ 0.15) with at least two independent evidence items, per the
  policy's own stop rule. Every answer carries a `stop_reason`.
- **Knows when it can't decide.** Inside the 0.35–0.65 band it returns `uncertain` and does *not* invent a customer
  reply. Three of the 20 exam cases end uncertain — that's a feature. An agent that always has an opinion is worse
  than one that escalates.
- **Requests evidence through controlled actions** — customer validation, step-up auth, analyst info — and records the
  assumed reply explicitly, because real replies aren't provided in this round.
- **Initial vs. final recommendation** with a `what_changed` line, so you can see the recommendation move as evidence
  arrives.
- **Only `auto` actions execute themselves.** `L1` and `L2` wait for a human, and fire through a mock action layer the
  moment one approves.
- **Explains itself**: every evidence item has a source and a query reference, every action cites its rule number, and
  the SAR narrative stands on its own for a regulator.

## 6. Results

Measured by replaying held-out October closed cases with the outcome hidden (dev models trained only on data before
2016-10-01). The exam itself has no answer key, so treat this as a proxy, not a forecast.

| Metric | Result |
|---|---|
| AUC (fraud vs legitimate alerts) | 0.934 |
| Brier score (calibration) | 0.096 |
| Fraud recall at p ≥ 0.5 | 0.868 |
| False-positive rate, cleared alerts | 0.08 |
| **False-positive rate, legitimate look-alikes** | **0.16 – 0.32** |
| Verdict accuracy on decided cases | 0.91 – 0.93 |
| Uncertain rate | 0.095 |
| Episode F1 (which transactions belong to the fraud) | 0.79 – 0.84 |
| Median exposure error | $0.00 |
| SAR decision accuracy | 0.941 |
| Final-action agreement with the bank (Jaccard) | 0.70 |

That bolded row is the uncomfortable one, and it's in the table on purpose. Against *legitimate look-alikes* —
unflagged transactions that happen to carry a high bank risk score — the agent still false-positives 16–32% of the
time depending on the sample. Half the exam cases are legitimate. An agent that blocks everything scores badly, and
this is the number that would bite in production.

## 7. What we learned

**1. The risk score is a trap for the model, too.** Negatives have to be matched to positives *within each risk band*.
Otherwise the model quietly learns "medium score means fraud," because every cleared alert in the data scores high.

**2. A silent sampling bug made the model look brilliant.** A DuckDB `USING SAMPLE` ran *before* the `WHERE`, starving
the negatives. AUC came back at 0.98 and the model was gloriously overconfident. The honest retrain scored **lower**
and was right. Check class balance, not just AUC.

**3. Fraud arrives in bursts.** Scoring transactions independently found 64% of fraud. Scoring them in context lifted
episode F1 from 0.52 to 0.88. Fraud isn't a transaction — it's an episode.

**4. Exposure decides the report.** The bank filed a SAR exactly when exposure exceeded $1,000 (393 of 393 cases) or
the pattern was undocumented (9 of 9). So getting the *episode* right — which transactions belong — is what drives the
scored report decision, not just detecting that something's wrong.

**5. Raw device sharing is noise.** Common devices sit on hundreds of cards. The ring signal isn't "shared device" —
it's a device that is rare **and** always marked New for the account **and** always behind an anonymous proxy.
Specificity is the whole game.

**6. Use exact rules where the labels are exact.** Channel mix and the `New` device flag determine four of the five
documented patterns perfectly. A classifier is only needed for in-person account takeover vs. out-of-region use
(~83% separable). Don't train a model to reproduce an `if` statement.

**7. A plausible rule that was wrong.** Our first recurring-charge test called a case a subscription off gaps of 20 and
25 days. Tightening it to genuinely monthly gaps removed the false positive. Plausible-looking heuristics need a
counter-check against cases they *shouldn't* fire on.

**8. The fact guard was right and my prompt was wrong.** We let the LLM reword summaries behind a guard: every number,
amount, date and ID must survive, or the template text is kept. The first real run had *every* rewrite rejected. The
cause was my prompt — I'd asked for "concise," so the models dropped facts to comply. Asking for a rewrite that keeps
everything, plus one repair round naming the missing values, took acceptance from 0% to 100%.

**9. We were stopping too early.** Our first ladder blocked at p ≥ 0.70 — before the policy's own stop rule of 0.85
with two independent evidence items. Moving the boundary halved wrongful blocks before verification (7% → 4% on
identical seeded samples) and made the recommendation visibly change after evidence arrives, with accuracy untouched.

**10. Not every rule in a policy generalises — and you should measure before implementing.** Policy rule R6 names three
shared origins: device profile, billing region, recipient email. We measured before wiring region in. Billing regions
in this dataset hold **90–580 active cards** in a 7-day window, and **8–16 of them always score ≥ 0.5** just from the
model's ordinary false-positive rate. There is no threshold that separates a real ring from a big city. Device
profiles work because they're a rare, specific fingerprint; a billing region is coarse geography shared by hundreds of
unrelated customers. So we implemented R6 for device-sharing only, and documented why, rather than ship a rule that
flags ordinary regional commerce as coordinated fraud. Shipping a rule you can't defend is worse than shipping less.

**11. Cross-checking two backends found a real bug.** Running all 20 cases on TigerGraph and diffing against the local
backend exposed a case-memory bug: a flag computed with `max()` was `NULL` for in-person-only cases (no device), so the
whole score went `NULL` and the row was silently dropped — meaning earlier fraud on the same card was missing from
`similar_prior_cases` for repeat victims. Fixed. It changed the memory lists of 16 answers and **no** verdict, pattern,
exposure or action. The two backends now agree exactly (0 differences over 100 field groups).

## 8. Limitations, said plainly

- **The fact guard checks numbers, not meaning.** A real model turned an observation ("risk scores stay low") into a
  causal claim ("chosen to keep them low"). SAR rewrites are rejected outright because models drop card IDs, so SARs
  stay on the checked template.
- **Simulated customer replies follow the agent's own belief.** They're recorded as assumptions, and they cannot
  validate the verdict — they're a consequence of it.
- **The ±72h window looks at transactions after the alert.** A live system must use only what was known at alert time.
- **R6 covers device sharing only.** Region was measured and dropped (lesson 10). Recipient email isn't in the graph
  schema at all — adding it means a schema change and full reload we chose not to risk near the deadline.
- **The dev set is a proxy.** It has no ground truth for the exam distribution.

## 9. What we'd do with more time

- **LLM-directed tool selection.** The planner is rule-based today. Let the model choose the next 1–2 tools inside the
  ambiguous band only, leaving the deterministic high- and low-confidence paths alone — and leaving the policy engine
  entirely untouched.
- **Time-honest evaluation**: data up to alert time only.
- **Generalised ring discovery** via community detection over the card-device graph, run continuously as a monitor
  rather than per-alert.
- **Per-pattern calibration** and an analyst feedback loop writing back into case memory.

## 10. Credits

Dataset: IEEE-CIS Fraud Detection (Vesta Corporation, via the IEEE Computational Intelligence Society), repackaged for
Hacker House Goa 2026 by TigerGraph. Only the provided dataset was used — the public Kaggle IEEE-CIS files were never
touched, which is both a rule and the only way the benchmark means anything.

Built on [TigerGraph](https://www.tigergraph.com) — thanks to [@TigerGraphDB](https://x.com/TigerGraphDB) for the task
and the Savanna workspace.

**Repo:** [github.com/neevmodh/graphsleuth](https://github.com/neevmodh/graphsleuth) ·
**Demo:** [graphsleuth-production.up.railway.app](https://graphsleuth-production.up.railway.app)
