# Data findings (Phase 1)

Facts verified against the provided dataset. Kaggle IEEE-CIS files were never used.

## Identifiers
- `card_id = <customer_id>-K<rank>` where rank orders each customer's distinct `(card4, card6)` pairs ascending, **NULLs first**. Verified on 14,975/14,975 card ids in `closed_cases_history.csv` + `case_pack.csv`. Implemented as the `cards` table / `tx` view in `data/load_local.py`.
- 14,318 cards, 13,553 customers. Some transactions have NULL `card4/card6` (that is a real card, usually K1).
- Device profile string (README): `DeviceInfo | id_30 | id_31 | id_33`. 144,432 transactions carry one.

## Time
- Case-pack `opened_at` = transaction `ts` + 6h (HHG-001: opened 01:55, txn 19:55 the day before). Reason in `ts`.
- Closed cases: `opened_at` is typically ~34h after the first fraud txn (min 2h, max ~65 days).

## Closed cases (labelled memory, 5,565)
CNP 1404 · account_takeover 1205 · CNP new device 1076 · out_of_region 955 · cleared 900 · card_testing 16 · undocumented 9.
Actions taken: `CREATE_CASE|BLOCK_CARD` 4268, `VERIFY_WITH_CUSTOMER|CLOSE_NO_FRAUD` 900 (all cleared), `+FILE_REPORT` 397.
Cleared reasons seen: traveller (billing region), new phone.

## The undocumented ring (HHG-014)
- Device profile `SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080`.
- 114 txns, 52 cards, every one `id_15=New` and `id_23=IP_PROXY:ANONYMOUS`, avg risk 0.13, amounts ~$36-$257, ProductCD C.
- Active in two bursts: 15 Aug–~10 Sep (labelled in closed cases) and 14 Nov–4 Dec (exam window). 4 closed `undocumented` cases sit on it.
- The bank's risk score barely reacts (HHG-014 flagged txn scores 0.05).

## Shared-device signal is noisy
Generic profiles (Windows 10 / Chrome 63, iOS Safari…) are shared by hundreds of cards. Ring detection therefore needs **rarity-weighted** device specificity (IDF-style), combined with `New` device + proxy + narrow time window, not a raw "cards per device" count.
Exam cases on small shared profiles worth checking: HHG-004 (6 cards), HHG-011 (4), HHG-015 (8), HHG-019 (5).
