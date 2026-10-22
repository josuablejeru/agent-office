"""A conversation made safe to hand to a different model."""

from __future__ import annotations

import json

from backend.providers.base import ChatMessage


def flatten_tool_turns(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Tool calls and results as plain text, which any model accepts.

    A model's tool turns can carry data only that model may see again (Claude's
    thinking blocks, Gemini's thought signatures). Another model is shown what
    happened in words instead. Screenshots are left out.
    """
    flat: list[ChatMessage] = []
    results: list[str] = []

    def flush() -> None:
        if results:
            flat.append(ChatMessage(role="user", content="Results of those actions:\n" + "\n".join(results)))
            results.clear()

    for message in messages:
        if message.role == "tool":
            note = " (a screenshot was taken; it is not shown here)" if message.images else ""
            results.append(f"- {message.content[:4000]}{note}")
            continue
        flush()
        if message.role == "assistant" and message.tool_calls:
            calls = "\n".join(f"- {call.name}({json.dumps(call.arguments)[:2000]})" for call in message.tool_calls)
            text = f"{message.content}\n\n" if message.content else ""
            flat.append(ChatMessage(role="assistant", content=f"{text}I took these actions:\n{calls}"))
        else:
            flat.append(message.model_copy(update={"provider_data": None}))
    flush()
    return flat
