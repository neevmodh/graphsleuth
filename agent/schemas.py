"""Answer-file schema (README "Answer Format"). Validation here mirrors what the
grader checks structurally, so a case that validates cannot be zeroed for shape."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Action = Literal[
    "ALLOW_TRANSACTION", "DECLINE_TRANSACTION", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS",
    "WARN_CUSTOMER", "VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH", "BLOCK_CARD", "BLOCK_ALL_CARDS",
    "GENERATE_REPORT", "CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD",
]
Route = Literal["auto", "L1", "L2"]
Pattern = Literal[
    "card_testing", "card_not_present_fraud", "card_not_present_new_device",
    "out_of_region_use", "account_takeover", "undocumented", "none",
]


class Evidence(BaseModel):
    claim: str
    source: Literal["graph", "document", "customer", "external"]
    ref: str
    entity_ids: list[str] = Field(default_factory=list)


class Case(BaseModel):
    status: Literal["open", "closed_fraud", "closed_legitimate", "escalated"]
    verdict: Literal["fraud", "legitimate", "uncertain"]
    fraud_probability: float = Field(ge=0, le=1)
    pattern: Pattern
    pattern_description: str = ""
    affected_txn_ids: list[str] = Field(default_factory=list)
    first_suspicious_txn_id: str = ""
    connected_card_ids: list[str] = Field(default_factory=list)
    connected_device_profiles: list[str] = Field(default_factory=list)
    exposure_usd: float = 0.0
    evidence: list[Evidence] = Field(default_factory=list)
    similar_prior_cases: list[str] = Field(default_factory=list)
    summary: str
    written_to_graph: bool = False
    graph_case_id: str = ""

    @model_validator(mode="after")
    def _checks(self) -> "Case":
        if self.pattern == "undocumented" and not self.pattern_description.strip():
            raise ValueError("pattern_description required when pattern is 'undocumented'")
        if self.verdict == "legitimate" and (self.affected_txn_ids or self.exposure_usd != 0):
            raise ValueError("legitimate verdict requires empty affected_txn_ids and exposure 0")
        return self


class EvidenceRequest(BaseModel):
    type: Literal["customer_validation", "step_up_auth", "analyst_info"]
    asked_after_step: int
    assumed_response: str


class ActionItem(BaseModel):
    action: Action
    route: Route
    reason: str


class NextBestActions(BaseModel):
    initial: list[ActionItem]
    final: list[ActionItem]
    what_changed: str


class Sar(BaseModel):
    file: bool
    reason: str
    narrative: str = ""
    subjects: list[str] = Field(default_factory=list)
    total_amount_usd: float = 0.0
    activity_dates: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _checks(self) -> "Sar":
        if self.file:
            if not self.narrative.strip():
                raise ValueError("sar.narrative required when file is true")
            if len(self.activity_dates) != 2:
                raise ValueError("sar.activity_dates must have two YYYY-MM-DD dates when file is true")
        else:
            if self.narrative or self.subjects or self.total_amount_usd or self.activity_dates:
                raise ValueError("when file is false: narrative '', subjects [], total 0, dates []")
        return self


class Answer(BaseModel):
    case_id: str
    case: Case
    evidence_requests: list[EvidenceRequest] = Field(default_factory=list)
    next_best_actions: NextBestActions
    sar: Sar
    stop_reason: str
    tool_calls: int = 0
    tokens: int = 0
    latency_s: float = 0.0

    @model_validator(mode="after")
    def _consistency(self) -> "Answer":
        final_actions = {a.action for a in self.next_best_actions.final}
        if self.sar.file != ("FILE_REPORT" in final_actions):
            raise ValueError("sar.file must agree with FILE_REPORT in next_best_actions.final")
        if not self.evidence_requests and self.next_best_actions.final != self.next_best_actions.initial:
            raise ValueError("no evidence requested: final must equal initial")
        return self
