"""Provider-neutral interfaces. Nothing outside `providers/` may use vendor types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProviderError(Exception):
    """A model request failed (network, authentication, malformed response)."""


class ToolCallRequest(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Set when the model produced arguments that could not be parsed.
    arguments_error: str | None = None


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    # Set on role="tool" messages: the call this message is the result of.
    tool_call_id: str | None = None
    # Opaque data a provider attached to its own assistant turn (see ModelResponse).
    provider_data: dict[str, Any] | None = None


class ToolSpec(BaseModel):
    name: str
    description: str
    # JSON Schema for the tool arguments.
    parameters: dict[str, Any]


class ModelResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    # Anything the provider needs to see again when this turn is sent back to it.
    # The agent loop passes it through without looking inside.
    provider_data: dict[str, Any] | None = None


class ModelProvider(ABC):
    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> ModelResponse: ...

    async def list_models(self) -> list[str]:
        """Model ids the provider offers, for the model picker."""
        return []


class DecisionResult(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)


class DecisionProvider(ABC):
    """Evaluates proposed agent actions (e.g. Jev). Not a conversational model."""

    @abstractmethod
    async def decide(
        self,
        state: dict[str, Any],
        questions: dict[str, Any],
    ) -> DecisionResult: ...
