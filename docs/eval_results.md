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
| False-positive rate, legitimate look-alikes | 0.24 |
| Verdict accuracy on decided cases | 0.913 |
| Uncertain rate | 0.095 |
| Pattern accuracy (fraud cases) | 0.844 |
| Episode F1 (which transactions belong to the fraud) | 0.836 |
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
   negatives once; the balanced retrain made the harness honest (legit look-alike FPR 24%, not hidden).
4. The pattern labels follow exact rules (channel mix, `id_15 = New`) except in-person account takeover vs
   out-of-region use (~83% separable); rules are used where they are exact and a classifier only for that pair.
5. Two undocumented patterns exist, both rule-detected: a rare-device ring across many cards, and threshold structuring
   (several purchases just under $500 within an hour).
