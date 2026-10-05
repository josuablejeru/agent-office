"""One small request that tells whether a provider and model can drive an agent."""

from __future__ import annotations

import asyncio
import time

from pydantic import BaseModel

from backend.providers.base import ChatMessage, ModelProvider, ProviderError, ToolSpec

TIMEOUT_SECONDS = 60.0
PROBE_TOOL = ToolSpec(
    name="report_status",
    description="Report the status of the connection test.",
    parameters={
        "type": "object",
        "properties": {"status": {"type": "string", "description": "Always the word ok."}},
        "required": ["status"],
    },
)
# The tool is asked for in words rather than forced: several current models
# reject a forced tool choice outright, which would make them look broken.
PROBE_MESSAGES = [
    ChatMessage(role="system", content="This is a connection test. Follow the instruction exactly."),
    ChatMessage(role="user", content='Call the report_status tool with status set to "ok".'),
]


class ModelCheck(BaseModel):
    # The provider answered at all.
    ok: bool
    # It also made the tool call that agents depend on.
    tools: bool = False
    seconds: float = 0.0
    detail: str


async def check_model(provider: ModelProvider, timeout: float = TIMEOUT_SECONDS) -> ModelCheck:
    started = time.monotonic()
    try:
        response = await asyncio.wait_for(provider.chat(PROBE_MESSAGES, [PROBE_TOOL]), timeout)
    except TimeoutError:
        return ModelCheck(
            ok=False,
            seconds=round(time.monotonic() - started, 1),
            detail=f"No answer within {timeout:.0f} seconds. A local model may still be loading; try again.",
        )
    except ProviderError as exc:
        return ModelCheck(ok=False, seconds=round(time.monotonic() - started, 1), detail=str(exc))
    seconds = round(time.monotonic() - started, 1)
    if any(call.name == PROBE_TOOL.name and not call.arguments_error for call in response.tool_calls):
        return ModelCheck(ok=True, tools=True, seconds=seconds, detail="Works, including tool calls.")
    return ModelCheck(
        ok=True,
        seconds=seconds,
        detail=(
            "The model answers, but it did not make a tool call. Agents need tool calls to use "
            "their computer, so this model is likely to be unreliable as an agent."
        ),
    )
