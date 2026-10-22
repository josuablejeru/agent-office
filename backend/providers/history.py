"""A conversation made safe to hand to a different model."""

from __future__ import annotations

import json

from backend.providers.base import ChatMessage


def flatten_tool_turns(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Tool calls and results told as a report, which any model accepts.

    A model's tool turns can carry data only that model may see again (Claude's
    thinking blocks, Gemini's thought signatures). Another model is told what
    happened instead, in a message from the user's side: written as its own
    turns, models tend to imitate them as text rather than call tools.
    Screenshots are left out.
    """
    flat: list[ChatMessage] = []
    steps: list[str] = []

    def flush() -> None:
        if not steps:
            return
        report = (
            "Here is what was done on this task so far:\n"
            + "\n".join(steps)
            + "\n\nContinue the task from here. Use the tools to act; do not repeat steps that failed."
        )
        steps.clear()
        if flat and flat[-1].role == "user":  # one user message, as every API accepts
            flat[-1] = flat[-1].model_copy(update={"content": f"{flat[-1].content}\n\n{report}"})
        else:
            flat.append(ChatMessage(role="user", content=report))

    for message in messages:
        if message.role == "tool":
            note = " (a screenshot was taken; not shown here)" if message.images else ""
            steps.append(f"   result: {message.content[:4000]}{note}")
            continue
        if message.role == "assistant" and message.tool_calls:
            if message.content:
                steps.append(f"- Noted: {message.content[:2000]}")
            for call in message.tool_calls:
                steps.append(f"- Action {len([x for x in steps if x.startswith('- Action')]) + 1}: "
                             f"{call.name} with {json.dumps(call.arguments)[:2000]}")
            continue
        flush()
        flat.append(message.model_copy(update={"provider_data": None}))
    flush()
    return flat
