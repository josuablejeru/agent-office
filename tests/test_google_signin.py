"""Finding Google Cloud credentials the way gcloud and Claude Code do, and saying why sign-in fails."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from google.auth import exceptions

from backend.providers import vertex
from backend.shell_env import import_login_environment, read_login_environment


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG", "HTTPS_PROXY", "GOOGLE_CLOUD_PROJECT"):
        monkeypatch.delenv(name, raising=False)


def fake_shell(tmp_path: Path, exports: str, noise: str = "") -> str:
    """A login shell whose startup prints things and sets variables, as a fish config might."""
    shell = tmp_path / "shell"
    shell.write_text(f"#!/bin/bash\n{noise}\n{exports}\nshift\neval \"$2\"\n")
    shell.chmod(shell.stat().st_mode | stat.S_IEXEC)
    return str(shell)


def test_variables_from_the_login_shell_are_taken_and_others_ignored(tmp_path: Path) -> None:
    shell = fake_shell(
        tmp_path,
        "export CLOUDSDK_CONFIG=/Users/me/.gcloud-work HTTPS_PROXY=http://proxy.corp:8080 SECRET_THING=nope",
        noise='echo "Welcome to fish, the friendly interactive shell"',
    )
    assert read_login_environment(shell)["CLOUDSDK_CONFIG"] == "/Users/me/.gcloud-work"
    added = import_login_environment(shell)
    assert sorted(added) == ["CLOUDSDK_CONFIG", "HTTPS_PROXY"]
    assert os.environ["HTTPS_PROXY"] == "http://proxy.corp:8080" and "SECRET_THING" not in os.environ


def test_what_is_already_set_wins_and_a_broken_shell_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDSDK_CONFIG", "/already/set")
    assert import_login_environment(fake_shell(tmp_path, "export CLOUDSDK_CONFIG=/from/shell")) == []
    assert os.environ["CLOUDSDK_CONFIG"] == "/already/set"
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/bash\nexit 1\n")
    broken.chmod(0o755)
    assert import_login_environment(str(broken)) == []
    assert import_login_environment(str(tmp_path / "missing-shell")) == []


def test_credentials_are_looked_for_where_gcloud_puts_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vertex, "ADC_USER_FILE", tmp_path / "home" / "application_default_credentials.json")
    assert not vertex.credentials_available()
    assert "gcloud's default folder" in vertex.credentials_summary()

    custom = tmp_path / "gcloud-work"
    custom.mkdir()
    (custom / "application_default_credentials.json").write_text(json.dumps(
        {"type": "external_account_authorized_user", "refresh_token": "never shown"}))
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(custom))
    assert vertex.credentials_available()
    summary = vertex.credentials_summary()
    assert "type external_account_authorized_user" in summary and "CLOUDSDK_CONFIG" in summary
    assert "never shown" not in summary

    explicit = tmp_path / "key.json"
    explicit.write_text(json.dumps({"type": "service_account"}))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(explicit))
    assert "type service_account" in vertex.credentials_summary()  # the explicit file comes first


def test_each_kind_of_failure_says_what_to_do() -> None:
    expired = vertex.describe_google_error(exceptions.RefreshError("invalid_grant: Bad Request"))
    assert "expired" in expired and "gcloud auth application-default login" in expired
    unreachable = vertex.describe_google_error(exceptions.TransportError("SSLError: certificate verify failed"))
    assert "could not be reached" in unreachable and "proxy or certificate" in unreachable
    missing = vertex.describe_google_error(exceptions.DefaultCredentialsError("not found"))
    assert "No usable Google Cloud credentials" in missing


def test_a_vertex_quota_error_passes_on_what_google_said() -> None:
    import asyncio

    import anthropic
    import httpx2 as httpx  # the HTTP library this Anthropic SDK is built on

    from backend.providers.anthropic import AnthropicProvider
    from backend.providers.base import ProviderError

    google_said = ("Quota exceeded for aiplatform.googleapis.com/online_prediction_requests_per_base_model "
                   "with base model: anthropic-claude. Please submit a quota increase request.")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"code": 429, "message": google_said, "status": "RESOURCE_EXHAUSTED"}})

    client = anthropic.AsyncAnthropic(api_key="x", base_url="http://vertex.test", max_retries=0,
                                      http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    provider = AnthropicProvider("claude-model", client=client, label="vertex")
    from backend.providers.base import ChatMessage

    with pytest.raises(ProviderError) as raised:
        asyncio.run(provider.chat([ChatMessage(role="user", content="hi")]))
    text = str(raised.value)
    assert "online_prediction_requests_per_base_model" in text and "region" in text


def test_model_names_for_the_vertex_provider_are_checked_and_completed() -> None:
    from backend.providers.base import ProviderError

    assert vertex.vertex_model_id("gemini-2.5-pro") == "google/gemini-2.5-pro"
    assert vertex.vertex_model_id("meta/llama-4") == "meta/llama-4"
    with pytest.raises(ProviderError, match="vertex-claude provider"):
        vertex.vertex_model_id("claude-opus-5-5")
    with pytest.raises(ProviderError, match="publisher/model"):
        vertex.vertex_model_id("mystery-model")
