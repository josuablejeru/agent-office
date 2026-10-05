"""Builds the policy engine from config.yaml."""

from __future__ import annotations

from backend.config import Settings
from backend.logging_config import get_logger
from backend.policy.actions import PolicyAction
from backend.policy.engine import PolicyEngine
from backend.providers.base import DecisionProvider
from backend.providers.jev import JevProvider, JevSettings

log = get_logger("policy")

# Ambiguous calls may be allowed or sent to the user; config cannot make them a silent reject.
DEFAULT_ACTIONS = {
    "allow": PolicyAction.ALLOW,
    "require_approval": PolicyAction.REQUIRE_APPROVAL,
}


def build_policy_engine(settings: Settings) -> PolicyEngine:
    config = settings.load_config()
    configured = str((config.get("policy") or {}).get("default_action", "allow"))
    if configured not in DEFAULT_ACTIONS:
        log.warning("unknown policy.default_action, requiring approval", extra={"value": configured})
    default_action = DEFAULT_ACTIONS.get(configured, PolicyAction.REQUIRE_APPROVAL)

    decision_provider: DecisionProvider | None = None
    if config.get("jev"):
        decision_provider = JevProvider(JevSettings.model_validate(config["jev"]))
        log.info("decision provider configured", extra={"provider": "jev"})
    return PolicyEngine(default_action, decision_provider)
