"""Mock action-execution layer.

The PDF allows "sending customer messages, freezing accounts, blocking cards, refunding
customers, updating CRM systems, or closing cases" to be simulated, stubbed, or represented
through mock APIs. `agent/policy.py` only ever *recommends* an action; this module is what
actually "does" it once an action clears its approval route (`auto` immediately, `L1`/`L2`
once a human approves it in the UI). Nothing here touches a real system -- each mock API is a
pure function returning a deterministic, timestamped receipt, so the same case always replays
the same execution log.
"""
from __future__ import annotations

import time
from typing import TypedDict


class ExecutionResult(TypedDict):
    action: str
    system: str
    status: str
    detail: str
    ref: str
    executed_at: str


_SYSTEM = {
    "DECLINE_TRANSACTION": "mock-card-network",
    "BLOCK_CARD": "mock-card-network",
    "BLOCK_ALL_CARDS": "mock-card-network",
    "STEP_UP_AUTH": "mock-auth-gateway",
    "VERIFY_WITH_CUSTOMER": "mock-messaging",
    "WARN_CUSTOMER": "mock-messaging",
    "MONITOR_CARD": "mock-fraud-monitor",
    "MONITOR_CONNECTED_CARDS": "mock-fraud-monitor",
    "CREATE_CASE": "mock-crm",
    "FILE_REPORT": "mock-regulatory-filing",
    "GENERATE_REPORT": "mock-regulatory-filing",
    "ESCALATE_TO_ANALYST": "mock-crm",
    "ALLOW_TRANSACTION": "mock-card-network",
    "CLOSE_NO_FRAUD": "mock-crm",
}

_DETAIL = {
    "DECLINE_TRANSACTION": "Pending authorization declined at the network.",
    "BLOCK_CARD": "Card flagged blocked; reissue queued.",
    "BLOCK_ALL_CARDS": "Every card on the customer flagged blocked; reissue queued for each.",
    "STEP_UP_AUTH": "Step-up authentication challenge issued to the cardholder's device.",
    "VERIFY_WITH_CUSTOMER": "Verification message sent to the cardholder on file.",
    "WARN_CUSTOMER": "Warning notice sent to the cardholder on file.",
    "MONITOR_CARD": "Card added to the elevated-monitoring watchlist.",
    "MONITOR_CONNECTED_CARDS": "Connected cards added to the elevated-monitoring watchlist.",
    "CREATE_CASE": "Case record created in the case-management system.",
    "FILE_REPORT": "Suspicious activity report submitted to the regulatory filing queue.",
    "GENERATE_REPORT": "Report generated and attached to the case record.",
    "ESCALATE_TO_ANALYST": "Case routed to the fraud-analyst queue.",
    "ALLOW_TRANSACTION": "Transaction released; no hold placed.",
    "CLOSE_NO_FRAUD": "Case closed as legitimate; no action taken against the account.",
}


def execute(action: str, case_id: str, card_id: str, route: str) -> ExecutionResult:
    """Simulate calling the mock system of record for `action`. Deterministic and side-effect-free
    beyond the caller persisting the returned receipt -- calling this twice for the same action
    just re-issues an equivalent receipt with a fresh timestamp, it never mutates shared state."""
    system = _SYSTEM.get(action, "mock-crm")
    return {
        "action": action,
        "system": system,
        "status": "executed",
        "detail": _DETAIL.get(action, f"{action} executed."),
        "ref": f"{system}:{case_id}:{card_id}:{action.lower()}",
        "executed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
