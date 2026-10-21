"""A stand-in for GitHub's "latest release" API and its downloads."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class FakeGitHub:
    def __init__(self) -> None:
        self.release: dict[str, Any] = {}
        self.files: dict[str, bytes | Path] = {}
        self.limited = False  # answer like GitHub's rate limit
        self.requests: list[dict[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, code: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
                self.send_response(code)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                outer.requests.append({"path": self.path, "if-none-match": self.headers.get("If-None-Match", "")})
                if self.path == "/repos/x/releases/latest":
                    if outer.limited:
                        return self._send(403, b'{"message": "API rate limit exceeded"}')
                    body = json.dumps(outer.release).encode()
                    etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
                    if self.headers.get("If-None-Match") == etag:
                        return self._send(304)
                    return self._send(200, body, {"ETag": etag, "Content-Type": "application/json"})
                name = self.path.rsplit("/", 1)[-1]
                if self.path.startswith("/download/") and name in outer.files:
                    data = outer.files[name]
                    body = data.read_bytes() if isinstance(data, Path) else data
                    return self._send(200, body, {"Content-Type": "application/octet-stream"})
                return self._send(404)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def latest_url(self) -> str:
        return f"{self.url}/repos/x/releases/latest"

    def publish(self, tag: str, archive: bytes | Path | None = None, checksum: str | None = None,
                notes: str = "## What's new\\n- Things", prerelease: bool = False) -> None:
        version = tag.removeprefix("v")
        name = f"Agent-Office-{version}-macos-arm64.zip"
        assets = []
        if archive is not None:
            data = archive.read_bytes() if isinstance(archive, Path) else archive
            self.files[name] = archive
            self.files[name + ".sha256"] = f"{checksum or hashlib.sha256(data).hexdigest()}  {name}\\n".encode()
            assets = [{"name": n, "browser_download_url": f"{self.url}/download/{tag}/{n}"} for n in (name, name + ".sha256")]
        self.release = {"tag_name": tag, "body": notes, "published_at": "2026-10-21T08:00:00Z",
                        "draft": False, "prerelease": prerelease, "assets": assets}

    def close(self) -> None:
        self._server.shutdown()
