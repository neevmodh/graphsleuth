# Dev-set results (held-out October closed cases)

`python -m eval.devset --n 50 -v` replays October closed cases with the outcome hidden. The dev models are trained only
on data before 2016-10-01, so the agent has never seen these labels. Sample: 50 confirmed-fraud cases per pattern,
50 cleared alerts, and 50 *legitimate look-alikes* (unflagged October transactions with a bank risk score >= 0.5).
The exam has no answer key, so this is the only measurement of accuracy we have; treat it as a proxy, not a forecast.

| Metric | Result |
|---|---|
| AUC (fraud vs legitimate alerts) | 0.934 |
| Brier score (calibration) | 0.096 |
| Fraud recall at p >= 0.5 | 0.868 |
| False-positive rate, cleared alerts | 0.08 |
| False-positive rate, legitimate look-alikes | 0.16 to 0.32 (varies by sample; 4 seeds) |
| Verdict accuracy on decided cases | 0.91 to 0.93 (4 seeds) |
| Uncertain rate | 0.095 |
| Pattern accuracy (fraud cases) | 0.844 |
| Episode F1 (which transactions belong to the fraud) | 0.79 to 0.84 (4 seeds) |
| Median exposure error | $0.00 |
| Report (SAR) decision accuracy | 0.941 |
| Final-action Jaccard vs the bank's own actions | 0.70 |

## Lessons that changed the design
1. Transaction-level scoring alone found only 64% of fraud: fraud arrives in bursts on one card, so a context model
   (neighbours, timing, shared device/region/product) lifted episode F1 from 0.52 to 0.88 on the first sample.
2. The bank filed a report **exactly** when exposure > $1,000 or the pattern was undocumented (393/393 and 9/9). Exposure
   accuracy therefore drives the report decision, which is why episode accuracy matters most.
3. Negatives must be matched to positives *within each bank-risk band*. Otherwise the model learns "medium risk score
   means fraud" (all cleared alerts score above 0.7). A `USING SAMPLE` placed before `WHERE` silently starved the
   negatives once; the balanced retrain made the harness honest (legit look-alike FPR of 16% to 32% depending on the sample, not hidden).
4. The pattern labels follow exact rules (channel mix, `id_15 = New`) except in-person account takeover vs
   out-of-region use (~83% separable); rules are used where they are exact and a classifier only for that pair.
5. Two undocumented patterns exist, both rule-detected: a rare-device ring across many cards, and threshold structuring
   (several purchases just under $500 within an hour).

## Verify before block (policy change)
The ladder used to block directly at p >= 0.70 with two independent evidence items. That stops before the policy's own
stop rule (section 6: p >= 0.85 with two independent items), so it now verifies first below 0.85. Measured on identical
seeded samples (seed 11): accuracy metrics are unchanged (the policy does not touch probabilities); a legitimate case
receiving a block or decline *before* verification fell from 7% to 4%; fraud cases whose recommendation changes after
evidence rose from 29% to 32%; action agreement with the bank's own actions was flat (0.716 to 0.718). On the 20 exam
cases exactly one changed (HHG-019, p = 0.80), with an identical final answer.

## A retrieval bug found by cross-checking backends
Running the 20 cases on TigerGraph and diffing against the local backend exposed a bug in the local case-memory query: a
flag computed with `max()` over a case's transactions was `NULL` for in-person-only cases (no device), so the score became
`NULL` and the row was silently dropped. Earlier fraud on the same card (a repeat victim) was missing from `similar_prior_cases`
for in-person cases. Fixed (flags default to 0; an entity link is required, the pattern only boosts rank). It changed the memory
lists of 16 answers and **no** verdict, pattern, exposure, affected transaction or final action; held-out metrics are unchanged.
After the fix the two backends agree exactly (0 differences over 100 field groups).
