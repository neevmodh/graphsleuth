import pytest
from pydantic import ValidationError

from agent.policy import Assessment, recommend, route_for, sar_required, stop_reached
from agent.schemas import ActionItem, Answer, Case, NextBestActions, Sar


def names(items):
    return [i.action for i in items]


def by_action(items):
    return {i.action: i for i in items}


# --- Section 2: routing -------------------------------------------------------
def test_routes():
    assert route_for("VERIFY_WITH_CUSTOMER") == "auto"
    assert route_for("DECLINE_TRANSACTION") == "L1"
    assert route_for("BLOCK_CARD", 2500.0) == "L1"      # <= $2,500
    assert route_for("BLOCK_CARD", 2500.01) == "L2"     # > $2,500
    assert route_for("BLOCK_ALL_CARDS") == "L2"
    assert route_for("FILE_REPORT") == "L2"


# --- R1: verify before blocking on a weak signal ------------------------------
def test_r1_weak_signal_verifies_not_blocks():
    a = Assessment(fraud_probability=0.45, exposure_usd=100, n_independent_evidence=1)
    out = names(recommend(a))
    assert "VERIFY_WITH_CUSTOMER" in out and "BLOCK_CARD" not in out
    assert "CREATE_CASE" in out            # 3a: evidence requested => case


# --- R2 / R3 / R4: customer response -------------------------------------------
def test_r2_denied_blocks_and_creates_case_no_report_when_small():
    a = Assessment(fraud_probability=0.5, exposure_usd=268.43)
    out = by_action(recommend(a, "denied"))
    assert set(out) == {"BLOCK_CARD", "CREATE_CASE"}
    assert out["BLOCK_CARD"].route == "L1"


def test_r2_denied_files_report_over_1000():
    a = Assessment(fraud_probability=0.5, exposure_usd=1000.03)
    assert "FILE_REPORT" in names(recommend(a, "denied"))


def test_r2_denied_shared_device_files_report_and_monitors_connected():
    a = Assessment(fraud_probability=0.5, exposure_usd=268.43, shared_origin=True,
                   connected_fraud_cards=["C00877-K1"])
    out = names(recommend(a, "denied"))
    assert {"BLOCK_CARD", "CREATE_CASE", "FILE_REPORT", "MONITOR_CONNECTED_CARDS"} <= set(out)


def test_r3_confirmed_closes():
    a = Assessment(fraud_probability=0.5, exposure_usd=100)
    assert names(recommend(a, "confirmed")) == ["CLOSE_NO_FRAUD"]


def test_r4_no_reply_monitors_declines_escalates_over_500():
    small = names(recommend(Assessment(fraud_probability=0.5, exposure_usd=300), "no_reply"))
    big = names(recommend(Assessment(fraud_probability=0.5, exposure_usd=600), "no_reply"))
    assert "MONITOR_CARD" in small and "DECLINE_TRANSACTION" in small
    assert "ESCALATE_TO_ANALYST" not in small and "ESCALATE_TO_ANALYST" in big


# --- R5: card testing -----------------------------------------------------------
def test_r5_card_testing_declines_and_steps_up():
    a = Assessment(fraud_probability=0.72, exposure_usd=268.43, card_testing=True, n_independent_evidence=1)
    out = by_action(recommend(a))
    assert {"DECLINE_TRANSACTION", "STEP_UP_AUTH"} <= set(out)
    assert out["DECLINE_TRANSACTION"].route == "L1"


def test_r5_card_testing_with_cleared_purchase_blocks():
    a = Assessment(fraud_probability=0.72, exposure_usd=268.43, card_testing=True, large_purchase_cleared=True)
    assert "BLOCK_CARD" in names(recommend(a))


# --- R6: shared origin -----------------------------------------------------------
def test_r6_shared_origin_report_and_monitor_connected():
    a = Assessment(fraud_probability=0.9, exposure_usd=200, n_independent_evidence=3,
                   shared_origin=True, connected_fraud_cards=["C1-K1", "C2-K1"])
    out = set(names(recommend(a)))
    assert {"CREATE_CASE", "FILE_REPORT", "MONITOR_CONNECTED_CARDS"} <= out


# --- R7: disputed but legitimate --------------------------------------------------
def test_r7_recurring_dispute_never_blocks():
    a = Assessment(fraud_probability=0.2, exposure_usd=15, trigger_type="customer_report", recurring_dispute=True)
    out = names(recommend(a))
    assert set(out) == {"CREATE_CASE", "VERIFY_WITH_CUSTOMER", "WARN_CUSTOMER"}


# --- R8: escalate when uncertain and exposed ---------------------------------------
def test_r8_uncertain_and_exposed_escalates():
    a = Assessment(fraud_probability=0.5, exposure_usd=650, n_independent_evidence=1)
    assert "ESCALATE_TO_ANALYST" in names(recommend(a))


def test_r8_evidence_conflict_escalates():
    a = Assessment(fraud_probability=0.5, exposure_usd=50, evidence_conflict=True)
    assert "ESCALATE_TO_ANALYST" in names(recommend(a))


# --- R9: undocumented coordinated pattern --------------------------------------------
def test_r9_undocumented_case_report_escalate():
    a = Assessment(fraud_probability=0.8, exposure_usd=75, pattern="undocumented", n_independent_evidence=2)
    out = set(names(recommend(a)))
    assert {"CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST"} <= out


# --- R10: never BLOCK_ALL_CARDS without cause --------------------------------------------
def test_r10_block_all_cards_is_never_emitted_without_cause():
    a = Assessment(fraud_probability=0.95, exposure_usd=5000, n_independent_evidence=3, confirmed_fraud_cards=1)
    assert "BLOCK_ALL_CARDS" not in names(recommend(a, "denied"))


# --- Case vs report (3a) and stop rule (6) ---------------------------------------------------
def test_sar_gate():
    assert not sar_required(Assessment(fraud_probability=0.9, exposure_usd=500))          # case only
    assert sar_required(Assessment(fraud_probability=0.9, exposure_usd=1200))
    assert sar_required(Assessment(fraud_probability=0.9, exposure_usd=100, shared_origin=True))
    assert not sar_required(Assessment(fraud_probability=0.4, exposure_usd=5000))          # not strongly suspected


def test_report_always_has_case():
    a = Assessment(fraud_probability=0.9, exposure_usd=2000, n_independent_evidence=3)
    out = names(recommend(a))
    assert "FILE_REPORT" in out and "CREATE_CASE" in out


def test_stop_rule():
    assert stop_reached(Assessment(fraud_probability=0.9, n_independent_evidence=2))
    assert not stop_reached(Assessment(fraud_probability=0.9, n_independent_evidence=1))
    assert stop_reached(Assessment(fraud_probability=0.1, n_independent_evidence=2))
    assert stop_reached(Assessment(fraud_probability=0.5), response="denied")
    assert not stop_reached(Assessment(fraud_probability=0.5))


def test_actions_ordered_by_what_happens_first():
    a = Assessment(fraud_probability=0.5, exposure_usd=1500)
    out = names(recommend(a, "denied"))
    assert out.index("BLOCK_CARD") < out.index("CREATE_CASE") < out.index("FILE_REPORT")


# --- Answer-schema consistency -----------------------------------------------------------------
def _case(**kw):
    base = dict(status="closed_fraud", verdict="fraud", fraud_probability=0.9, pattern="card_testing",
                affected_txn_ids=["1"], exposure_usd=10.0, summary="s")
    return Case(**{**base, **kw})


def _nba(final_extra=()):
    ini = [ActionItem(action="VERIFY_WITH_CUSTOMER", route="auto", reason="R1")]
    fin = ini + list(final_extra)
    return NextBestActions(initial=ini, final=fin, what_changed="x")


def test_answer_sar_must_match_file_report():
    sar_off = Sar(file=False, reason="no")
    with pytest.raises(ValidationError):
        Answer(case_id="HHG-001", case=_case(), evidence_requests=[], next_best_actions=_nba(
            [ActionItem(action="FILE_REPORT", route="L2", reason="R2")]), sar=sar_off, stop_reason="x")


def test_answer_undocumented_needs_description():
    with pytest.raises(ValidationError):
        _case(pattern="undocumented", pattern_description="")


def test_legitimate_must_be_empty():
    with pytest.raises(ValidationError):
        _case(verdict="legitimate", affected_txn_ids=["1"])


def test_sar_false_fields_must_be_empty():
    with pytest.raises(ValidationError):
        Sar(file=False, reason="no", narrative="oops")


# --- verify-then-close flow, customer reports and R7 outcome -------------------------------------------
def test_legit_leaning_alert_verifies_then_closes():
    a = Assessment(fraud_probability=0.10, exposure_usd=0, n_independent_evidence=3)
    assert names(recommend(a)) == ["VERIFY_WITH_CUSTOMER"]          # no case below the 0.30 gate
    assert names(recommend(a, "confirmed")) == ["CLOSE_NO_FRAUD"]   # R3


def test_customer_report_supported_by_evidence_is_r2():
    a = Assessment(fraud_probability=0.77, exposure_usd=55, trigger_type="customer_report", n_independent_evidence=3)
    assert {"BLOCK_CARD", "CREATE_CASE"} <= set(names(recommend(a)))


def test_customer_report_contradicted_by_evidence_verifies_and_escalates():
    a = Assessment(fraud_probability=0.05, exposure_usd=40, trigger_type="customer_report", evidence_conflict=True)
    out = set(names(recommend(a)))
    assert "BLOCK_CARD" not in out
    assert {"VERIFY_WITH_CUSTOMER", "CREATE_CASE", "ESCALATE_TO_ANALYST"} <= out


def test_r7_confirmed_keeps_record_and_reminder():
    a = Assessment(fraud_probability=0.1, trigger_type="customer_report", recurring_dispute=True)
    assert set(names(recommend(a, "confirmed"))) == {"CREATE_CASE", "WARN_CUSTOMER", "CLOSE_NO_FRAUD"}
