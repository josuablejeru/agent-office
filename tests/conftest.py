"""Shared test setup."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_real_google_credentials(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Google sign-in finds nothing: no test may use the developer's Google Cloud credentials.

    Opening Settings lists every provider's models, and for Vertex AI that
    asks Google with whatever credentials this Mac has.
    """
    home = tmp_path_factory.mktemp("no-google")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(Path(home) / "missing.json"))
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(home))
