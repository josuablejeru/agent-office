"""Anthropic provider, built on the official SDK."""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic

from backend.logging_config import get_logger
from backend.providers.base import (
    OMITTED_IMAGE_NOTE,
    ChatMessage,
    ModelProvider,
    ModelResponse,
    ProviderError,
    ToolCallRequest,
    ToolSpec,
    images_to_send,
)

log = get_logger("providers")

DEFAULT_MAX_TOKENS = 16_000
# Key under which a turn's original content blocks travel through the agent loop.
RAW_CONTENT = "anthropic_content"


def to_tool(tool: ToolSpec) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}


def _assistant_content(message: ChatMessage) -> Any:
    # Replay the model's own blocks unchanged when we have them: thinking blocks
    # must come back exactly as they were produced.
    raw = (message.provider_data or {}).get(RAW_CONTENT)
    if raw:
        return raw
    blocks: list[dict[str, Any]] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    blocks += [
        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        for call in message.tool_calls
    ]
    return blocks or message.content


def _tool_result(message: ChatMessage, with_images: bool = True) -> dict[str, Any]:
    content: Any = message.content
    if message.images:
        if with_images:
            content = [{"type": "text", "text": message.content}] + [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}}
                for image in message.images
            ]
        else:
            content = f"{message.content}\n{OMITTED_IMAGE_NOTE}"
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": message.tool_call_id,
        "content": content,
    }
    try:
        if "error" in json.loads(message.content):
            block["is_error"] = True
    except (ValueError, TypeError):
        pass
    return block


def to_request(messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
    """Split neutral messages into Anthropic's system prompt and message list."""
    system = "\n\n".join(m.content for m in messages if m.role == "system")
    wire: list[dict[str, Any]] = []
    sent_images = images_to_send(messages)
    for index, message in enumerate(messages):
        if message.role == "system":
            continue
        if message.role == "tool":
            # All results for one assistant turn go back in a single user message.
            result = _tool_result(message, with_images=index in sent_images)
            previous = wire[-1] if wire else None
            if previous and previous["role"] == "user" and isinstance(previous["content"], list):
                previous["content"].append(result)
            else:
                wire.append({"role": "user", "content": [result]})
        elif message.role == "assistant":
            wire.append({"role": "assistant", "content": _assistant_content(message)})
        else:
            wire.append({"role": "user", "content": message.content})
    return system, wire


def parse_message(message: Any) -> ModelResponse:
    if message.stop_reason == "refusal":
        category = getattr(getattr(message, "stop_details", None), "category", None)
        raise ProviderError(
            "The model declined this request" + (f" ({category})." if category else ".")
        )
    text_parts: list[str] = []
    calls: list[ToolCallRequest] = []
    for block in message.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            arguments = block.input if isinstance(block.input, dict) else {}
            calls.append(ToolCallRequest(id=block.id, name=block.name, arguments=arguments))
    if message.stop_reason == "max_tokens" and calls:
        # A tool call cut off by the output limit has incomplete arguments.
        calls[-1].arguments_error = "the tool call was cut off by the output token limit"
    return ModelResponse(
        text="".join(text_parts).strip(),
        tool_calls=calls,
        provider_data={RAW_CONTENT: message.to_dict()["content"]} if calls else None,
    )


class AnthropicProvider(ModelProvider):
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client: Any = None,
        label: str = "anthropic",
    ) -> None:
        self._label = label
        self._model = model
        self._max_tokens = max_tokens
        # Without an explicit key the SDK resolves credentials itself
        # (environment variables or an `ant auth login` profile).
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)

    async def _call(self, request: Any) -> Any:
        try:
            return await request
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise ProviderError(
                "The credentials were rejected, or they lack access to this model."
            ) from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError(f"The model '{self._model}' was not found.") from exc
        except anthropic.RateLimitError as exc:
            # On Vertex this is usually a quota of zero for this model in this region,
            # not too many requests: pass on what the API says.
            said = " ".join(str(getattr(exc, "message", "") or exc).split())[:400]
            hint = ""
            if self._label == "vertex":
                hint = (
                    " On Vertex AI this usually means the project has no quota for this model in this "
                    "region: try the region Claude Code uses (CLOUD_ML_REGION), such as the multi-regions "
                    "eu or us, or request quota in the Google Cloud console (IAM & Admin › Quotas)."
                )
            raise ProviderError(f"Rate limit or quota reached: {said}.{hint}") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"The API returned HTTP {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            cause = exc.__cause__ or exc.__context__
            reason = f" ({type(cause).__name__}: {' '.join(str(cause).split())[:200]})" if cause else ""
            raise ProviderError(f"Could not reach the model API{reason}.") from exc
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - e.g. Google credential refresh on Vertex
            # Claude on Vertex signs in to Google Cloud on the first request.
            from google.auth.exceptions import GoogleAuthError

            if isinstance(exc, GoogleAuthError):
                from backend.providers.vertex import describe_google_error

                raise ProviderError(describe_google_error(exc)) from exc
            raise ProviderError(f"The model request failed: {exc}") from exc

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> ModelResponse:
        system, wire = to_request(messages)
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": wire,
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = [to_tool(tool) for tool in tools]
        started = time.monotonic()
        response = parse_message(await self._call(self._client.messages.create(**params)))
        log.info(
            "model request",
            extra={
                "provider": self._label,
                "model": self._model,
                "messages": len(messages),
                "tool_calls": len(response.tool_calls),
                "seconds": round(time.monotonic() - started, 2),
            },
        )
        return response

    async def list_models(self) -> list[str]:
        if self._label != "anthropic":
            return []  # only the first-party API lists models
        page = await self._call(self._client.models.list(limit=100))
        return sorted(model.id for model in page.data)
