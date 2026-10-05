"""Policy decision types shared by the rules, the engine and the agent loop."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class PolicyAction(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    REJECT = "reject"


class PolicyDecision(BaseModel):
    action: PolicyAction
    risk: str = "low"
    reason: str = ""
    # Which layer decided: "rule", "jev" or "default".
    source: str = "default"
