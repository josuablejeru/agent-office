"""Choosing the model for a task, and moving to a stronger one when it gets stuck.

Two ideas, borrowed from how others route between models:
- **Routes in plain words**, matched by a small, fast model (as Arch-Router and
  LiteLLM's LLM classifier do): "quick lookups -> Haiku", "coding -> Opus".
  One decision per task, so the model stays the same within it (provider-side
  caches keep working).
- **Escalation** (a cascade): when the chosen model gets stuck, the rest of
  the task goes to a stronger one, once. It is stuck when it says so itself
  (the ask_for_stronger_model tool) or after several actions in a row failed.

The router sees only what the user wrote: never tool results or web pages,
which could otherwise steer which model is used.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from backend.config import Settings
from backend.logging_config import get_logger
from backend.providers.base import ChatMessage, ModelProvider, ModelResponse, ProviderError, ToolSpec
from backend.providers.history import flatten_tool_turns

log = get_logger("routing")

ROUTER_TIMEOUT_SECONDS = 20.0
ROUTER_INPUT_CHARS = 3000
ESCALATE_OPERATION = "model.escalate"
ESCALATE_TOOL = "ask_for_stronger_model"
# Results that mean "not allowed", not "it went wrong": they never count as failures.
NOT_FAILURES = ("The user rejected this action", "This action was blocked by policy")


class ModelRef(BaseModel):
    label: str
    provider: str
    model: str


class Route(BaseModel):
    when: str = Field(min_length=3, max_length=300)
    use: str = Field(min_length=1, max_length=40)  # a name from the list of models


class RoutingSettings(BaseModel):
    """config.yaml `routing`: which model chooses, the routes, and where to escalate."""

    router: str = ""  # a name from the list of models
    routes: list[Route] = Field(default_factory=list, max_length=12)
    escalate_to: str = ""  # a name from the list, or "" for no escalation
    escalate_after: int = Field(default=3, ge=2, le=10)

    @property
    def enabled(self) -> bool:
        return bool(self.router and self.routes)


def model_list(settings: Settings) -> list[ModelRef]:
    found = []
    for entry in settings.load_config().get("models") or []:
        try:
            found.append(ModelRef.model_validate(entry))
        except ValueError:
            continue
    return found


def load_routing(settings: Settings) -> RoutingSettings:
    try:
        return RoutingSettings.model_validate(settings.load_config().get("routing") or {})
    except ValueError:
        return RoutingSettings()


def find(models: list[ModelRef], label: str) -> ModelRef | None:
    return next((model for model in models if model.label.lower() == label.strip().lower()), None)


ROUTER_PROMPT = """You choose which AI model should handle a task for an office assistant.
Read the user's request and pick the route that fits it best.

Routes:
{routes}

Answer with the route's number only. Answer 0 if none fits."""


def router_messages(routing: RoutingSettings, user_texts: list[str]) -> list[ChatMessage]:
    routes = "\n".join(f"{index}. {route.when}" for index, route in enumerate(routing.routes, start=1))
    # The latest request matters most; earlier ones give context. Only the user's own words.
    request = "\n\n".join(text.strip() for text in user_texts[-3:] if text.strip())[-ROUTER_INPUT_CHARS:]
    return [
        ChatMessage(role="system", content=ROUTER_PROMPT.format(routes=routes)),
        ChatMessage(role="user", content=f"The request:\n<request>\n{request}\n</request>\n\nRoute number:"),
    ]


def parse_route(text: str, count: int) -> int | None:
    """The route number in the router's answer, or None when it is not a clear one."""
    numbers = re.findall(r"\b\d+\b", re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL))
    if len(set(numbers)) != 1:
        return None
    number = int(numbers[0])
    return number if 0 <= number <= count else None


async def choose_model(
    routing: RoutingSettings,
    models: list[ModelRef],
    user_texts: list[str],
    build: Callable[[str, str], ModelProvider],
    timeout: float = ROUTER_TIMEOUT_SECONDS,
) -> tuple[ModelRef | None, str]:
    """The model for a task and why, or (None, why) to use the agent's own model."""
    router = find(models, routing.router)
    if router is None:
        return None, f"the router model '{routing.router}' is not in the list"
    try:
        answer = await asyncio.wait_for(
            build(router.provider, router.model).chat(router_messages(routing, user_texts), None), timeout
        )
    except TimeoutError:
        return None, f"{router.label} did not choose within {timeout:.0f} s"
    except ProviderError as exc:
        return None, f"{router.label} could not choose ({str(exc)[:120]})"
    number = parse_route(answer.text, len(routing.routes))
    if number is None:
        return None, f"{router.label}'s answer was not a route number"
    if number == 0:
        return None, "no route fits"
    route = routing.routes[number - 1]
    target = find(models, route.use)
    if target is None:
        return None, f"route {number} uses '{route.use}', which is not in the list"
    return target, f"“{route.when}”"


# --- moving to another model within a task ------------------------------------------------------


def is_failure(message: ChatMessage) -> bool:
    """A tool result where the action went wrong (not one the user or a rule refused)."""
    try:
        result = json.loads(message.content)
    except ValueError:
        return False
    if not isinstance(result, dict):
        return False
    error = str(result.get("error") or "")
    if error:
        return not error.startswith(NOT_FAILURES)
    if result.get("timed_out"):
        return True
    # Exit code 1 is often an answer, not a failure: grep found nothing, test said no, diff saw a difference.
    code = result.get("exit_code")
    return isinstance(code, int) and code >= 2


def failures_in_a_row(messages: list[ChatMessage]) -> int:
    count = 0
    for message in reversed(messages):
        if message.role == "assistant":
            continue  # the calls between results
        if message.role != "tool" or not is_failure(message):
            break
        count += 1
    return count


ESCALATE_SPEC = ToolSpec(
    name=ESCALATE_TOOL,
    description=(
        "Hand the rest of this task to a stronger model. Use it when the task turns out to be harder "
        "than you can handle well: you are stuck, unsure, or keep making mistakes. Say why briefly."
    ),
    parameters={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
)


class EscalatingProvider(ModelProvider):
    """The task's model, which hands over to a stronger one at most once."""

    def __init__(
        self,
        current: ModelProvider,
        stronger: Callable[[], ModelProvider],
        stronger_label: str,
        after_failures: int,
        on_escalate: Callable[[str], None],
    ) -> None:
        self._current = current
        self._stronger = stronger
        self.stronger_label = stronger_label
        self._after = after_failures
        self._on_escalate = on_escalate
        self._requested: str | None = None
        self._cut: int | None = None  # messages before this are shown as text to the stronger model

    @property
    def escalated(self) -> bool:
        return self._cut is not None

    async def request(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """The ask_for_stronger_model tool."""
        if self.escalated:
            return {"error": f"You are already the stronger model ({self.stronger_label}). Carry on."}
        self._requested = str(arguments.get("reason") or "it asked for help")[:200]
        return {"result": f"{self.stronger_label} takes over the rest of this task."}

    def _escalate(self, messages: list[ChatMessage], why: str) -> None:
        self._current = self._stronger()
        self._cut = len(messages)
        log.info("escalated", extra={"to": self.stronger_label, "why": why})
        self._on_escalate(f"escalated to {self.stronger_label}: {why}")

    async def chat(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None) -> ModelResponse:
        if not self.escalated:
            if self._requested is not None:
                self._escalate(messages, self._requested)
            elif failures_in_a_row(messages) >= self._after:
                self._escalate(messages, f"{self._after} actions in a row failed")
        if self._cut is None:
            return await self._current.chat(messages, tools)
        # The stronger model has nothing to escalate to.
        tools = [tool for tool in tools or [] if tool.name != ESCALATE_TOOL] or None
        return await self._current.chat(flatten_tool_turns(messages[: self._cut]) + messages[self._cut :], tools)
