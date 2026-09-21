# GraphSleuth: demo script (target 4:30, limit 5:00)

Every number below comes from the current `cases/*.json` outputs. Re-check them against the files if you re-run the agent.

## Before you record
- [ ] **Backend badge.** The UI badge must read what is true. Today it says `backend: local DuckDB`. Record the
      TigerGraph narration **only after** the Savanna backend is connected and the badge says `backend: TigerGraph`
      and cases show `written to TigerGraph: yes`. Until then, do not say the agent "queries TigerGraph".
- [ ] `python run_cases.py` then `python -m eval.validate_answers cases` (expect 20/20 valid).
- [ ] `python -m uvicorn api.main:app --port 8000`, open http://localhost:8000, dark theme, browser zoom 110%, window 1440 px wide.
- [ ] The live stream is paced at 0.35 s per step (`?pace=0.35` in `ui/index.html`) so viewers can read each tool call.
- [ ] Do a dry run of each scene once. Clear approvals for a clean take: delete `data/store/approvals.json`.
- [ ] Say once, out loud, that customer replies are **simulated** (the task provides none) and recorded as assumptions.

## Scene 0: the problem (0:00 to 0:25)
**Screen:** queue of 20 cases, mixed verdict chips.
> "Fraud teams get alerts faster than they can investigate them. A risk score says *look here*, but it is wrong in both
> directions: in this dataset every cleared false alarm scored above 0.7, and some real fraud scored near zero.
> GraphSleuth is an agent that investigates each alert, decides how sure it is, and recommends the next best action,
> inside the bank's policy and approval limits."

## Scene 1: a scary score that is not fraud, HHG-010 (0:25 to 1:20)
**Screen:** click HHG-010, press *Investigate*. Trigger: risk score 0.90, $1,000.03, online.
1. Narrate the steps as they stream: transaction, card profile, ±72 h window, context model, device, recurring check, similar cases.
2. Point at the evidence: *amount is 14.5x the card's median; device never seen on this card.* "That looks bad."
3. Point at the gauge: **fraud probability 0.02**. "But the model has learned from the bank's own closed cases that
   a new-device, high-score purchase like this is almost always a customer on a new phone."
4. Actions: **before evidence** `VERIFY_WITH_CUSTOMER` (auto). **After** an assumed customer confirmation: `CLOSE_NO_FRAUD` (auto).
> "It does not block a customer on one alarming signal. It asks, and closes the alert. No SAR: the report rule needs
> confirmed or strongly suspected fraud."

## Scene 2: honest uncertainty, HHG-001 (1:20 to 2:05)
**Screen:** HHG-001. Trigger: risk score 0.61, $77.07, in person.
- Gauge sits **inside the 0.35 to 0.65 band** (p = 0.61): *uncertain*.
- Actions change: **before** `VERIFY_WITH_CUSTOMER`, `CREATE_CASE`; **after** no reply is assumed (R4):
  `DECLINE_TRANSACTION` (L1, needs a team lead), `CREATE_CASE`, `MONITOR_CARD`.
> "When the evidence is genuinely ambiguous, the agent does not invent a customer reply. It records that none came
> back and takes the cautious, reversible actions. The decline needs a human lead, so it waits."
- Click **Approve** on `DECLINE_TRANSACTION`. "Only auto actions run without a person."

## Scene 3: the fraud ring nobody scored, HHG-014 (2:05 to 3:35): the centrepiece
**Screen:** HHG-014. Trigger text: an *analyst* noticed several cards using the same unusual device.
1. "The flagged transaction's risk score is **0.05**. On its own it is nothing."
2. Steps: `device_neighbors` (device on 52 cards, 100% New, 100% behind an anonymous proxy), `device_burst`
   (**28 cards** in one burst, 14 Nov to 4 Dec), `similar_cases` (CC-3035, CC-2985, CC-2971).
3. **Evidence graph:** "The shared device is in the middle; the connected cards form a ring around it." Point at
   the closed cases in green: "the agent remembered earlier confirmed cases on the same device."
4. **Actions:** `BLOCK_CARD` (L1), `CREATE_CASE`, `ESCALATE_TO_ANALYST`, `MONITOR_CONNECTED_CARDS` (27 cards),
   `FILE_REPORT` (**L2**, fraud manager). Click Approve on the report. "The agent recommended; a manager decides."
5. Scroll to the **SAR narrative**: who, what, when, where, how, why, naming the 27 connected cards.
> "The pattern is undocumented: no single transaction looks alarming. It only appears by linking cards through the device."

## Scene 4: a second undocumented pattern, HHG-006 (3:35 to 4:05)
**Screen:** HHG-006 (customer report, $482.12).
- "Four online purchases in half an hour, each just under $500, about 8x the card's normal amount: **$1,906.07** total.
  This looks like structuring under an authorization threshold. Not in the bank's five documented patterns."
- Actions include `FILE_REPORT` (rule R9) and `ESCALATE_TO_ANALYST`. Closed cases CC-4124, CC-4086, CC-3907 are cited as memory.

## Scene 5: close (4:05 to 4:30)
**Screen:** README or `docs/eval_results.md` table.
> "On held-out cases the agent never saw: 91% verdict accuracy on decided cases, 87% fraud recall, and it identifies
> which transactions belong to the fraud well enough that the median exposure error is zero dollars, which is what
> decides whether a report is required. It also tells you when it is unsure. Built on TigerGraph for Hacker House Goa."
- Do **not** quote a number you have not re-run. Quote the "legit look-alike" false-positive rate (24%) if asked.

## If asked / common questions
- *Are the customer replies real?* No. Simulated, and recorded under `evidence_requests.assumed_response`.
- *Why does no fraud case show verify then block?* Strong cases meet the stop rule (p at or above 0.85 with two
  independent pieces of evidence) and go straight to block. The verify-then-deny path exists and is unit-tested; it does
  not occur in these 20 cases.
- *Does it look at the future?* Its window is ±72 h around the alert, and the benchmark data has all of it. A live
  deployment would use only transactions up to the alert time.

## Shot list and timing
| Time | Scene | Case | Must show |
|---|---|---|---|
| 0:00 | Problem | queue | verdict chips |
| 0:25 | False alarm | HHG-010 | streamed steps, gauge 0.02, verify then close |
| 1:20 | Uncertainty | HHG-001 | band, no assumed reply, L1 approval |
| 2:05 | Ring | HHG-014 | graph, L2 report, SAR narrative |
| 3:35 | Structuring | HHG-006 | $1,906.07, R9 |
| 4:05 | Close | docs | held-out numbers |
