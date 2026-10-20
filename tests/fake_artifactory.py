"""A stand-in for a company JFrog Artifactory: PyPI repositories behind a username and token.

It serves one small package of its own (agent-office-probe) and, for anything
else, passes the request on to the public PyPI index, the way a "remote"
repository in Artifactory does. Every request is recorded.
"""

from __future__ import annotations

import base64
import hashlib
import io
import threading
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROBE_WHEEL = "agent_office_probe-1.0-py3-none-any.whl"


def probe_wheel() -> bytes:
    files = {
        "agent_office_probe/__init__.py": "MESSAGE = 'served by the company repository'\n",
        "agent_office_probe-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: agent-office-probe\nVersion: 1.0\n",
        "agent_office_probe-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "".join(f"{name},,\n" for name in files) + "agent_office_probe-1.0.dist-info/RECORD,,\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        for name, text in {**files, "agent_office_probe-1.0.dist-info/RECORD": record}.items():
            wheel.writestr(name, text)
    return buffer.getvalue()


class FakeArtifactory:
    def __init__(
        self, username: str, token: str, repository: str = "pypi-remote", host: str = "127.0.0.1",
        tls: tuple[str, str] | None = None,
    ) -> None:
        self.requests: list[dict[str, Any]] = []
        expected = "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()
        wheel = probe_wheel()
        digest = hashlib.sha256(wheel).hexdigest()
        prefix = f"/artifactory/api/pypi/{repository}"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, code: int, body: bytes = b"", content_type: str = "text/html") -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                if code == 401:
                    self.send_header("WWW-Authenticate", 'Basic realm="Artifactory"')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                authorized = self.headers.get("Authorization") == expected
                outer.requests.append({"path": self.path, "authorized": authorized})
                if not authorized:
                    return self._send(401)
                if not self.path.startswith(prefix + "/"):
                    return self._send(404)
                rest = self.path[len(prefix):]
                if rest.rstrip("/") == "/simple/agent-office-probe":
                    link = f'<a href="{prefix}/packages/{PROBE_WHEEL}#sha256={digest}">{PROBE_WHEEL}</a>'
                    return self._send(200, f"<html><body>{link}</body></html>".encode())
                if rest == f"/packages/{PROBE_WHEEL}":
                    return self._send(200, wheel, "application/octet-stream")
                if rest.startswith("/simple/"):
                    # A remote repository: the public index's answer, files still on PyPI's servers.
                    try:
                        with urllib.request.urlopen("https://pypi.org" + rest, timeout=20) as upstream:
                            return self._send(200, upstream.read(), upstream.headers.get("Content-Type", "text/html"))
                    except Exception:  # noqa: BLE001
                        return self._send(404)
                return self._send(404)

        self._server = ThreadingHTTPServer((host, 0), Handler)
        self.port = self._server.server_address[1]
        self.scheme = "http"
        if tls:
            import ssl

            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
            self.scheme = "https"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def url(self, host: str = "127.0.0.1") -> str:
        return f"{self.scheme}://{host}:{self.port}/artifactory"

    def paths(self, authorized: bool = True) -> list[str]:
        return [r["path"] for r in self.requests if r["authorized"] == authorized]

    def close(self) -> None:
        self._server.shutdown()
