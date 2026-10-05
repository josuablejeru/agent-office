"""Generic OpenAI-compatible chat provider (Ollama, xAI, OpenRouter, LM Studio, vLLM)."""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import httpx

from backend.logging_config import get_logger
from backend.providers.base import (
    ChatMessage,
    ModelProvider,
    ModelResponse,
    ProviderError,
    ToolCallRequest,
    ToolSpec,
)

log = get_logger("providers")

# Local models can take minutes to load before the first token.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)
# Some reasoning models emit their scratchpad inline; it is not part of the answer.
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
# Key under which a turn's original tool calls travel through the agent loop.
RAW_TOOL_CALLS = "openai_tool_calls"
KEEP_RECENT_TOOL_RESULTS = 2
SHORTENED_RESULT_CHARS = 300


def to_wire_message(message: ChatMessage) -> dict[str, Any]:
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    raw_calls = (message.provider_data or {}).get(RAW_TOOL_CALLS)
    if raw_calls:
        # Sent back exactly as received: some models attach data to their tool
        # calls (Gemini's thought signatures) that must be returned untouched.
        wire["tool_calls"] = raw_calls
    elif message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        wire["tool_call_id"] = message.tool_call_id
    return wire


def fit_context(wire: list[dict[str, Any]], budget_chars: int) -> list[dict[str, Any]]:
    """Shorten the oldest tool results until the conversation fits the budget.

    Local servers silently drop the *start* of an over-long prompt, which loses
    the system prompt and the task. Old page snapshots are the cheapest thing to
    give up instead; the most recent results are always kept whole.
    """
    def size() -> int:
        return sum(len(str(message.get("content") or "")) for message in wire)

    tool_indexes = [i for i, message in enumerate(wire) if message["role"] == "tool"]
    for index in tool_indexes[:-KEEP_RECENT_TOOL_RESULTS]:
        if size() <= budget_chars:
            break
        content = str(wire[index].get("content") or "")
        if len(content) > SHORTENED_RESULT_CHARS:
            wire[index] = {
                **wire[index],
                "content": content[:SHORTENED_RESULT_CHARS] + " ...[older result shortened]",
            }
    return wire


def to_wire_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def parse_tool_call(raw: dict[str, Any]) -> ToolCallRequest:
    function = raw.get("function") or {}
    call_id = raw.get("id") or f"call_{uuid.uuid4().hex[:12]}"
    name = str(function.get("name") or "")
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return ToolCallRequest(
                id=call_id, name=name, arguments_error=f"arguments are not valid JSON: {exc}"
            )
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return ToolCallRequest(
            id=call_id, name=name, arguments_error="arguments must be a JSON object"
        )
    return ToolCallRequest(id=call_id, name=name, arguments=arguments)


def parse_response(payload: dict[str, Any]) -> ModelResponse:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError("provider returned a response without a message") from exc
    text = THINK_BLOCK.sub("", message.get("content") or "").strip()
    raw_calls = [dict(raw) for raw in message.get("tool_calls") or []]
    calls = [parse_tool_call(raw) for raw in raw_calls]
    for raw, call in zip(raw_calls, calls, strict=True):
        raw["id"] = call.id  # ids we had to generate must match what we send back
    return ModelResponse(
        text=text,
        tool_calls=calls,
        provider_data={RAW_TOOL_CALLS: raw_calls} if raw_calls else None,
    )


def error_message(response: httpx.Response) -> str:
    """The provider's own description of what went wrong, without the JSON around it."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        error = None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])[:500]
    if isinstance(error, str) and error:
        return error[:500]
    return response.text.strip()[:500] or "no details given"


class OpenAICompatibleProvider(ModelProvider):
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        context_chars: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self._context_chars = context_chars
        self._extra_body = extra_body or {}
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    def _wire_tool(self, tool: ToolSpec) -> dict[str, Any]:
        return to_wire_tool(tool)

    async def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        headers = await self._auth_headers()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport, headers=headers
            ) as client:
                response = await client.request(method, f"{self._base_url}{path}", json=body)
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.ConnectError):
                raise ProviderError(
                    f"Could not connect to the model server at {self._base_url}. Is it running?"
                ) from exc
            raise ProviderError(
                f"request to {self._base_url} failed: {exc or type(exc).__name__}"
            ) from exc
        if response.status_code >= 400:
            # Provider error bodies describe the problem and never echo the key.
            raise ProviderError(
                f"{self._base_url} answered with an error ({response.status_code}): "
                f"{error_message(response)}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"{self._base_url} returned a non-JSON response") from exc

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> ModelResponse:
        wire = [to_wire_message(message) for message in messages]
        if self._context_chars:
            wire = fit_context(wire, self._context_chars)
        body: dict[str, Any] = {**self._extra_body, "model": self._model, "messages": wire}
        if tools:
            body["tools"] = [self._wire_tool(tool) for tool in tools]
        started = time.monotonic()
        payload = await self._request("POST", "/chat/completions", body)
        response = parse_response(payload)
        log.info(
            "model request",
            extra={
                "base_url": self._base_url,
                "model": self._model,
                "messages": len(messages),
                "tool_calls": len(response.tool_calls),
                "seconds": round(time.monotonic() - started, 2),
            },
        )
        return response

    async def list_models(self) -> list[str]:
        payload = await self._request("GET", "/models", None)
        return sorted(str(item["id"]) for item in payload.get("data") or [] if "id" in item)
