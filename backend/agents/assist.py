"""Help with creating a coworker: a job description, and a drawn avatar, written by a model.

Both use the model chosen in the agent form, so they work with any provider.
The avatar is SVG drawn by the model: it is untrusted markup, so it is parsed
safely and reduced to plain shapes before anyone sees it.
"""

from __future__ import annotations

import asyncio
import re
from xml.etree.ElementTree import Element, tostring

from defusedxml import ElementTree as SafeElementTree

from backend.providers.base import ChatMessage, ModelProvider, ProviderError

JOB_DESCRIPTION_CHARS = 2_000
SVG_MAX_BYTES = 30_000
TIMEOUT_SECONDS = 120

JOB_DESCRIPTION_PROMPT = """You write job descriptions for AI coworkers in a small office. A job description is
read by the coworker itself before every task, so write it to them, in the second person.

Write 3 to 5 short sentences, plain text, no heading, no list, no quotes:
- start with "You are" and the role;
- say what they are good at and what they look after;
- say how they work (for example: careful, checks sources, keeps answers short, asks colleagues
  in the shared channel when something is outside their job).

Example: You are the office accountant. You are precise with numbers, show your calculations and
double-check totals before reporting them."""

AVATAR_PROMPT = """You draw small, friendly avatars for coworkers in an office app, as SVG.

Rules:
- Reply with one <svg> element only, nothing before or after it: no explanation, no code fence.
- <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">.
- Flat colours, simple shapes (circle, ellipse, rect, path, polygon), no text, no images, no filters.
- A round coloured background, and a cartoon person from the shoulders up, facing forward:
  face, eyes, a smile, hair, clothes. Add one or two details that fit the role or description.
- A made-up person: never a likeness of a real or famous person or a known character."""


def clean_text(text: str) -> str:
    """The model's answer without code fences, quotes or leading labels."""
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text.strip())
    text = re.sub(r"^(job description|description)\s*:\s*", "", text, flags=re.IGNORECASE)
    return text.strip().strip('"').strip()


async def write_job_description(provider: ModelProvider, name: str, role: str) -> str:
    """A job description for a coworker called `name` whose role the user described as `role`."""
    request = f"The coworker is called {name or 'unnamed'}. What the user wants them to do: {role.strip()}"
    try:
        response = await asyncio.wait_for(provider.chat([
            ChatMessage(role="system", content=JOB_DESCRIPTION_PROMPT),
            ChatMessage(role="user", content=request),
        ]), TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise ProviderError(f"The model did not answer within {TIMEOUT_SECONDS} seconds.") from exc
    text = clean_text(response.text)
    if not text:
        raise ProviderError("The model gave no answer; try again or choose another model.")
    return text[:JOB_DESCRIPTION_CHARS]


# --- avatars --------------------------------------------------------------------------------------

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
ALLOWED_ELEMENTS = {
    "svg", "g", "circle", "ellipse", "rect", "path", "polygon", "polyline", "line",
    "defs", "linearGradient", "radialGradient", "stop", "clipPath",
}
ALLOWED_ATTRIBUTES = {
    "viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
    "d", "points", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "opacity",
    "fill-opacity", "stroke-opacity", "fill-rule", "clip-rule", "transform", "id", "offset",
    "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "fx", "fy", "clip-path",
}
# Paint and references may only point inside the drawing.
SAFE_VALUE = re.compile(r"^[#a-zA-Z0-9.,%()\s+-]*$")
LOCAL_REFERENCE = re.compile(r"url\(\s*#[A-Za-z][\w-]*\s*\)")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def sanitize_svg(markup: str) -> str | None:
    """Plain shapes only, or None when nothing usable is left.

    Kept: the elements and attributes listed above. Dropped: scripts, styles,
    event handlers, embedded images or documents, links, and any reference to
    something outside the drawing.
    """
    found = re.search(r"<svg\b.*?</svg>", markup, re.DOTALL | re.IGNORECASE)
    if not found or len(found.group(0).encode()) > SVG_MAX_BYTES * 3:
        return None
    try:
        root = SafeElementTree.fromstring(found.group(0))
    except Exception:  # noqa: BLE001 - anything unparsable is simply not used
        return None
    if _local(root.tag) != "svg":
        return None

    def copy(element: Element) -> Element | None:
        tag = _local(element.tag)
        if tag not in ALLOWED_ELEMENTS:
            return None
        clean = Element(tag)
        for name, value in element.attrib.items():
            name = _local(name)
            if name not in ALLOWED_ATTRIBUTES:
                continue
            if "url(" in value:
                if not LOCAL_REFERENCE.fullmatch(value.strip()):
                    continue
            elif not SAFE_VALUE.match(value):
                continue
            clean.set(name, value)
        for child in element:
            copied = copy(child)
            if copied is not None:
                clean.append(copied)
        return clean

    clean = copy(root)
    if clean is None or not len(clean):
        return None
    clean.set("xmlns", SVG_NAMESPACE)
    clean.set("viewBox", clean.get("viewBox") or "0 0 256 256")
    for size in ("width", "height"):
        clean.set(size, "256")
    text = tostring(clean, encoding="unicode")
    return text if len(text.encode()) <= SVG_MAX_BYTES else None


async def draw_avatars(provider: ModelProvider, description: str, count: int = 3) -> list[str]:
    """Up to `count` avatar drawings for a coworker described by `description`."""
    async def one(attempt: int) -> str | None:
        try:
            response = await asyncio.wait_for(provider.chat([
                ChatMessage(role="system", content=AVATAR_PROMPT),
                ChatMessage(role="user", content=f"Draw variant {attempt + 1} for: {description.strip()}"),
            ]), TIMEOUT_SECONDS)
        except (TimeoutError, ProviderError):
            return None
        return sanitize_svg(response.text)

    # One after another: local model servers mostly answer one request at a time anyway.
    drawings = []
    for attempt in range(count):
        drawing = await one(attempt)
        if drawing:
            drawings.append(drawing)
    if not drawings:
        raise ProviderError("The model did not produce a usable drawing; try again or choose another model.")
    return drawings
