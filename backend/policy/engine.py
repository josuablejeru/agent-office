"""Policy engine: deterministic rules first, a decision provider only for what is left."""

from __future__ import annotations

from typing import Any

from backend.logging_config import get_logger
from backend.policy.actions import PolicyAction, PolicyDecision
from backend.policy.permissions import GROUP_NAMES, group_of
from backend.policy.rules import classify
from backend.providers.base import DecisionProvider, ProviderError

log = get_logger("policy")

__all__ = ["PolicyAction", "PolicyDecision", "PolicyEngine"]

DECISION_QUESTIONS = {
    "allow_execution": "boolean: may this action run at all?",
    "requires_approval": "boolean: must the user approve it first?",
    "action_risk": "one of: low, medium, high",
    "action_category": "short label for the kind of action",
    "reason": "one sentence for the user",
}


class PolicyEngine:
    """Combines hardcoded rules with an optional decision provider (Jev).

    A rule's verdict is final. The decision provider is consulted only for calls
    no rule covers, so it can never loosen a hardcoded rule.
    """

    def __init__(
        self,
        default_action: PolicyAction = PolicyAction.ALLOW,
        decision_provider: DecisionProvider | None = None,
    ) -> None:
        self._default_action = default_action
        self._decision_provider = decision_provider

    async def evaluate(
        self,
        operation: str,
        arguments: dict[str, Any],
        task: str = "",
        use_decision_provider: bool = False,
        level: str = "allow",
        unknown_action: PolicyAction | None = None,
    ) -> PolicyDecision:
        """Decide one call. `level` and `unknown_action` are the agent's own settings.

        The agent's settings can only tighten: "off" refuses, "ask" turns anything
        that would run by itself into an approval, and "allow" changes nothing, so
        a rule that asks for approval still does.
        """
        group = GROUP_NAMES.get(group_of(operation) or "", "This kind of action")
        if level == "off":
            return PolicyDecision(
                action=PolicyAction.REJECT, reason=f"{group} is switched off for this agent.", source="agent"
            )
        decision = await self._decide(operation, arguments, task, use_decision_provider, unknown_action)
        if level == "ask" and decision.action == PolicyAction.ALLOW:
            return PolicyDecision(
                action=PolicyAction.REQUIRE_APPROVAL,
                risk=decision.risk,
                reason=f"You chose to confirm every action of the kind \"{group}\" for this agent.",
                source="agent",
            )
        return decision

    async def _decide(
        self,
        operation: str,
        arguments: dict[str, Any],
        task: str,
        use_decision_provider: bool,
        unknown_action: PolicyAction | None,
    ) -> PolicyDecision:
        verdict = classify(operation, arguments)
        if verdict.action is not None:
            return PolicyDecision(
                action=verdict.action, risk=verdict.risk, reason=verdict.reason, source="rule"
            )
        if use_decision_provider and self._decision_provider is not None:
            decision = await self._ask_provider(operation, arguments, task)
            if decision is not None:
                return decision
        if unknown_action is not None:
            return PolicyDecision(action=unknown_action, source="agent")
        return PolicyDecision(action=self._default_action, source="default")

    @property
    def default_action(self) -> PolicyAction:
        return self._default_action

    async def _ask_provider(
        self, operation: str, arguments: dict[str, Any], task: str
    ) -> PolicyDecision | None:
        assert self._decision_provider is not None
        state = {"task": task, "tool": operation, "arguments": arguments}
        try:
            result = await self._decision_provider.decide(state, DECISION_QUESTIONS)
        except ProviderError as exc:
            # The app must keep working without Jev (for example when offline).
            log.warning("decision provider unavailable", extra={"error": str(exc)})
            return None
        answers = result.answers
        risk = str(answers.get("action_risk", "medium")).lower()
        reason = str(answers.get("reason", ""))[:300]
        if answers.get("allow_execution") is False:
            action = PolicyAction.REJECT
        elif answers.get("requires_approval") is True or risk == "high":
            action = PolicyAction.REQUIRE_APPROVAL
        elif answers.get("allow_execution") is True:
            action = PolicyAction.ALLOW
        else:
            return None  # no usable answer
        log.info("decision provider ruled", extra={"tool": operation, "action": action})
        return PolicyDecision(action=action, risk=risk, reason=reason, source="jev")
