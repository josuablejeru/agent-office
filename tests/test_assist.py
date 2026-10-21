"""AI help when creating a coworker: job descriptions and drawn avatars."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.agents.assist import draw_avatars, sanitize_svg, write_job_description
from backend.providers.base import ChatMessage, ModelProvider, ModelResponse, ProviderError, ToolSpec


class Scripted(ModelProvider):
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.requests: list[list[ChatMessage]] = []

    async def chat(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None) -> ModelResponse:
        self.requests.append(messages)
        assert tools is None  # no tools: this is writing, not acting
        return ModelResponse(text=self.answers.pop(0) if self.answers else "")


def test_a_job_description_is_written_to_the_coworker() -> None:
    model = Scripted('```\nJob description: "You are the purchasing assistant. You track supplier invoices."\n```')
    text = asyncio.run(write_job_description(model, "pia", "handles supplier invoices"))
    assert text == "You are the purchasing assistant. You track supplier invoices."
    assert "pia" in model.requests[0][-1].content and "handles supplier invoices" in model.requests[0][-1].content
    assert "second person" in model.requests[0][0].content


def test_no_answer_is_said_plainly_and_long_ones_are_cut() -> None:
    with pytest.raises(ProviderError, match="no answer"):
        asyncio.run(write_job_description(Scripted(""), "x", "something"))
    assert len(asyncio.run(write_job_description(Scripted("You are " + "very " * 1000), "x", "y"))) == 2000


SAFE = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><circle cx="128" cy="128" r="120" fill="#2f6fed"/></svg>'


@pytest.mark.parametrize("hostile", [
    '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><circle r="5"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><circle r="5"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><image href="http://evil.example/x.png"/><circle r="5"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><b>x</b></foreignObject><circle r="5"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><style>*{background:url(http://e)}</style><circle r="5"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5" fill="url(http://evil.example/p#a)"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)"><circle r="5"/></a><circle r="5"/></svg>',
])
def test_drawings_are_reduced_to_plain_shapes(hostile: str) -> None:
    clean = sanitize_svg(hostile)
    assert clean is not None and clean.startswith("<svg")
    body = clean.replace('xmlns="http://www.w3.org/2000/svg"', "")  # the namespace is the one allowed address
    for forbidden in ("onload", "script", "image", "foreignObject", "style", "http", "javascript", "<a"):
        assert forbidden not in body


def test_unusable_drawings_are_refused() -> None:
    assert sanitize_svg("sorry, I cannot draw") is None
    assert sanitize_svg('<svg xmlns="http://www.w3.org/2000/svg"></svg>') is None  # nothing left
    # A DOCTYPE before the drawing is cut away; an entity inside it cannot be resolved.
    outside = ('<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
               '<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>')
    assert "ENTITY" not in (sanitize_svg(outside) or "")
    inside = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="1">&x;</circle></svg>'
    assert sanitize_svg(inside) is None
    big = '<svg xmlns="http://www.w3.org/2000/svg">' + '<circle r="1"/>' * 10_000 + "</svg>"
    assert sanitize_svg(big) is None
    assert sanitize_svg("Here is your avatar:\n```svg\n" + SAFE + "\n```") is not None  # chatter around it is fine


def test_drawings_keep_only_the_usable_ones() -> None:
    drawings = asyncio.run(draw_avatars(Scripted(SAFE, "no drawing", SAFE), "an accountant", count=3))
    assert len(drawings) == 2
    with pytest.raises(ProviderError, match="usable drawing"):
        asyncio.run(draw_avatars(Scripted("x", "y"), "anything", count=2))


def test_the_helpers_are_reachable_through_the_api(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from backend.api import assist as assist_api
    from backend.config import Settings
    from backend.keystore import KeyStore
    from backend.main import create_app
    from tests.test_agents_api import FakeKeychain

    monkeypatch.setattr(assist_api, "build_provider", lambda *args: Scripted("You are the IT helper."))
    app = create_app(Settings(home=tmp_path / "home"), key_store=KeyStore(run=FakeKeychain()))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["Authorization"] = f"Bearer {app.state.api_token}"
        written = client.post("/api/assist/job-description",
                              json={"provider": "ollama", "model": "m", "name": "ben", "role": "fixes printers"})
        assert written.json() == {"text": "You are the IT helper."}
        monkeypatch.setattr(assist_api, "build_provider", lambda *args: Scripted(SAFE))
        drawn = client.post("/api/assist/avatar",
                            json={"provider": "ollama", "model": "m", "description": "IT helper", "count": 1})
        assert drawn.json()["candidates"][0].startswith("<svg")
        missing = client.post("/api/assist/job-description",
                              json={"provider": "nope", "model": "m", "role": "fixes printers"})
        assert missing.status_code == 400 and "not configured" in missing.json()["detail"]
