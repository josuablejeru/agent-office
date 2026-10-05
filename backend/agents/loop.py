"""The tool-calling loop. Knows nothing about specific providers, VMs or the database."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel

from backend.policy.actions import PolicyAction, PolicyDecision
from backend.providers.base import (
    ChatMessage,
    ModelProvider,
    ToolCallRequest,
    ToolSpec,
)


class RunLimits(BaseModel):
    max_tool_calls_per_run: int = 50
    max_runtime_seconds: float = 900
    max_repeated_identical_calls: int = 3


class StopReason(StrEnum):
    COMPLETED = "completed"
    TOOL_LIMIT = "tool_limit"
    REPEAT_LIMIT = "repeat_limit"
    TIMEOUT = "timeout"


class LoopResult(BaseModel):
    text: str
    stop_reason: StopReason


class Outcome(StrEnum):
    """What finally happened to a proposed tool call."""

    ALLOWED = "allow"
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    INVALID = "invalid"


class ToolExecutor(Protocol):
    def operation_for(self, tool_name: str) -> str | None: ...

    async def execute(self, call: ToolCallRequest) -> dict[str, Any]: ...


class RunObserver(Protocol):
    """How a run reports progress and asks its surroundings for decisions."""

    async def tool_started(self, call: ToolCallRequest, operation: str) -> int:
        """Record a proposed call; returns an id used in the calls below."""

    async def request_approval(self, record_id: int, decision: PolicyDecision) -> bool:
        """Ask the user. Returns True for "allow once"."""

    async def wait_until_agent_may_act(self) -> None:
        """Block while the user has manual control of the agent's computer."""

    async def tool_finished(self, record_id: int, outcome: Outcome, result: dict[str, Any]) -> None: ...


Evaluate = Callable[[str, dict[str, Any]], Awaitable[PolicyDecision]]

STOP_MESSAGES = {
    StopReason.TOOL_LIMIT: "Stopped: this run reached its limit of {limit} tool calls.",
    StopReason.REPEAT_LIMIT: (
        "Stopped: the same tool call was repeated more than {limit} times without progress."
    ),
    StopReason.TIMEOUT: "Stopped: this run exceeded its time limit of {limit:g} seconds.",
}


def call_signature(call: ToolCallRequest) -> str:
    return call.name + ":" + json.dumps(call.arguments, sort_keys=True)


class RunClock:
    """The run's time limit, which only counts time the agent itself spends."""

    def __init__(self, scope: asyncio.Timeout) -> None:
        self._scope = scope

    @asynccontextmanager
    async def paused(self) -> AsyncIterator[None]:
        """Stop the clock while waiting for the user (approval, manual control)."""
        now = asyncio.get_running_loop().time
        deadline = self._scope.when()
        remaining = None if deadline is None else max(deadline - now(), 0.0)
        self._scope.reschedule(None)
        try:
            yield
        finally:
            if remaining is not None:
                self._scope.reschedule(now() + remaining)


async def _handle_call(
    call: ToolCallRequest,
    executor: ToolExecutor,
    evaluate: Evaluate,
    observer: RunObserver,
    clock: RunClock,
) -> dict[str, Any]:
    """Take one proposed call through policy, approval and execution."""
    operation = executor.operation_for(call.name) or call.name
    record_id = await observer.tool_started(call, operation)
    if call.arguments_error:
        result = {"error": call.arguments_error}
        await observer.tool_finished(record_id, Outcome.INVALID, result)
        return result

    decision = await evaluate(operation, call.arguments)
    outcome = Outcome.ALLOWED
    if decision.action == PolicyAction.REJECT:
        outcome = Outcome.BLOCKED
    elif decision.action == PolicyAction.REQUIRE_APPROVAL:
        async with clock.paused():
            approved = await observer.request_approval(record_id, decision)
        outcome = Outcome.APPROVED if approved else Outcome.REJECTED

    if outcome in (Outcome.ALLOWED, Outcome.APPROVED):
        async with clock.paused():
            await observer.wait_until_agent_may_act()
        result = await executor.execute(call)
    elif outcome == Outcome.REJECTED:
        result = {"error": "The user rejected this action. Do not retry it; ask how to proceed."}
    else:
        reason = decision.reason or "not permitted by policy"
        result = {"error": f"This action was blocked by policy: {reason}"}
    await observer.tool_finished(record_id, outcome, result)
    return result


async def _loop(
    provider: ModelProvider,
    messages: list[ChatMessage],
    tools: list[ToolSpec],
    executor: ToolExecutor,
    evaluate: Evaluate,
    limits: RunLimits,
    observer: RunObserver,
    clock: RunClock,
) -> LoopResult:
    seen: Counter[str] = Counter()
    executed = 0
    while True:
        response = await provider.chat(messages, tools or None)
        if not response.tool_calls:
            return LoopResult(text=response.text, stop_reason=StopReason.COMPLETED)
        messages.append(
            ChatMessage(
                role="assistant",
                content=response.text,
                tool_calls=response.tool_calls,
                provider_data=response.provider_data,
            )
        )
        for call in response.tool_calls:
            if executed >= limits.max_tool_calls_per_run:
                return _stopped(StopReason.TOOL_LIMIT, limits.max_tool_calls_per_run)
            seen[call_signature(call)] += 1
            if seen[call_signature(call)] > limits.max_repeated_identical_calls:
                return _stopped(StopReason.REPEAT_LIMIT, limits.max_repeated_identical_calls)
            executed += 1
            result = await _handle_call(call, executor, evaluate, observer, clock)
            messages.append(
                ChatMessage(role="tool", content=json.dumps(result), tool_call_id=call.id)
            )


def _stopped(reason: StopReason, limit: float) -> LoopResult:
    return LoopResult(text=STOP_MESSAGES[reason].format(limit=limit), stop_reason=reason)


async def run_agent_loop(
    provider: ModelProvider,
    messages: list[ChatMessage],
    tools: list[ToolSpec],
    executor: ToolExecutor,
    evaluate: Evaluate,
    limits: RunLimits,
    observer: RunObserver,
) -> LoopResult:
    """Drive the model until it answers without tool calls or a limit is hit.

    `messages` is extended in place with the assistant and tool turns of this run.
    """
    try:
        async with asyncio.timeout(limits.max_runtime_seconds) as scope:
            return await _loop(
                provider, messages, tools, executor, evaluate, limits, observer, RunClock(scope)
            )
    except TimeoutError:
        return _stopped(StopReason.TIMEOUT, limits.max_runtime_seconds)
