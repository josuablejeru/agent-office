"""The Shared folder: transfers between the host and an agent's computer."""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api.files import SAFE_NAME, free_path
from backend.config import Settings
from backend.keystore import KeyStore
from backend.main import create_app
from backend.vm.guest import GuestClient
from backend.vm.transfer import OUTDATED_DAEMON, TransferError, download, explain, list_files, upload
from guest import shared
from guest.agentd import serve
from guest.validation import OperationError
from tests.test_agents_api import FakeKeychain, payload


@pytest.fixture
def shared_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "Shared"
    monkeypatch.setattr(shared, "SHARED_DIR", folder)
    return folder


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_round_trip_through_the_real_daemon(shared_dir: Path, tmp_path: Path) -> None:
    payload_bytes = os.urandom(2_500_000)  # several chunks

    async def scenario() -> None:
        port = free_port()
        server = asyncio.create_task(serve("127.0.0.1", port, "s3cret"))
        try:
            client = GuestClient(port, "s3cret")
            await client.wait_ready(timeout=5, interval=0.05)
            result = await upload(client, "report final.bin", payload_bytes)
            assert result == {"path": "report final.bin", "size": len(payload_bytes)}
            await upload(client, "empty.txt", b"")

            listed = {entry["path"]: entry["size"] for entry in await list_files(client)}
            assert listed == {"report final.bin": len(payload_bytes), "empty.txt": 0}

            destination = tmp_path / "back.bin"
            assert await download(client, "report final.bin", destination) == len(payload_bytes)
            assert hashlib.sha256(destination.read_bytes()).digest() == hashlib.sha256(payload_bytes).digest()

            with pytest.raises(TransferError, match="no such file"):
                await download(client, "missing.bin", tmp_path / "x")
            assert not (tmp_path / "x").exists()
        finally:
            server.cancel()

    run(scenario())
    assert (shared_dir / "report final.bin").read_bytes() == payload_bytes
    assert not list(shared_dir.glob(".*.partial"))


def test_damaged_upload_is_discarded(shared_dir: Path) -> None:
    run(shared.shared_write_chunk({"name": "a.txt", "offset": 0, "data": "aGVsbG8="}))
    with pytest.raises(OperationError, match="damaged"):
        run(shared.shared_finish({"name": "a.txt", "sha256": "0" * 64}))
    assert list(shared_dir.iterdir()) == []
    with pytest.raises(OperationError, match="no upload"):
        run(shared.shared_finish({"name": "a.txt", "sha256": "0" * 64}))


@pytest.mark.parametrize("name", ["../escape.txt", "a/b.txt", ".hidden", "", "..", "/etc/passwd"])
def test_upload_names_cannot_leave_the_folder(shared_dir: Path, name: str) -> None:
    with pytest.raises(OperationError):
        run(shared.shared_write_chunk({"name": name, "offset": 0, "data": ""}))


def test_downloads_cannot_leave_the_folder(shared_dir: Path, tmp_path: Path) -> None:
    shared_dir.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    (shared_dir / "link").symlink_to(secret)
    (shared_dir / "sub").mkdir()
    (shared_dir / "sub" / "ok.txt").write_text("fine")
    for path in ("../secret.txt", "link", "/etc/passwd", "sub/../../secret.txt"):
        with pytest.raises(OperationError):
            run(shared.shared_stat({"path": path}))
    assert run(shared.shared_stat({"path": "sub/ok.txt"}))["size"] == 4
    listed = [entry["path"] for entry in run(shared.shared_list({}))["files"]]
    assert listed == ["sub/ok.txt"]  # the symlink target is outside, so it is not offered


def test_oversized_chunks_are_refused(shared_dir: Path) -> None:
    with pytest.raises(OperationError, match="25 MB"):
        run(shared.shared_write_chunk({"name": "big", "offset": shared.MAX_FILE_BYTES, "data": "aGk="}))
    with pytest.raises(TransferError, match="25 MB"):
        run(upload(None, "big", b"x" * (25 * 1024**2 + 1)))  # type: ignore[arg-type]


def test_old_daemons_get_a_helpful_message() -> None:
    assert str(explain(TransferError("unknown operation: 'shared.list'"))) == OUTDATED_DAEMON
    assert "boom" in str(explain(TransferError("boom")))


def test_download_names_never_overwrite(tmp_path: Path) -> None:
    assert free_path(tmp_path, "report.pdf") == tmp_path / "report.pdf"
    (tmp_path / "report.pdf").touch()
    (tmp_path / "report (1).pdf").touch()
    assert free_path(tmp_path, "report.pdf") == tmp_path / "report (2).pdf"


def test_file_api_guards(tmp_path: Path) -> None:
    assert SAFE_NAME.match("Quarterly report (final).pdf") and SAFE_NAME.match("résumé.docx")
    for bad in (".env", "a/b", "a\\b", "", "x" * 201):
        assert not SAFE_NAME.match(bad)

    settings = Settings(home=tmp_path / "home")
    app = create_app(settings, key_store=KeyStore(run=FakeKeychain()))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["Authorization"] = f"Bearer {app.state.api_token}"
        agent_id = client.post("/api/agents", json=payload()).json()["id"]
        # the computer is off
        assert client.get(f"/api/agents/{agent_id}/files").status_code == 409
        assert client.put(f"/api/agents/{agent_id}/files/a.txt", content=b"hi").status_code == 409
        assert client.post(f"/api/agents/{agent_id}/saved-files", json={"path": "a.txt"}).status_code == 409
        assert client.put(f"/api/agents/{agent_id}/files/.env", content=b"x").status_code == 400
        too_big = client.put(f"/api/agents/{agent_id}/files/big.bin", content=b"0" * (25 * 1024**2 + 1))
        assert too_big.status_code == 413
        assert client.get("/api/agents/999/files").status_code == 404
