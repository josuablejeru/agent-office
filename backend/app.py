"""Desktop entry point: runs the backend and shows the UI in a native window.

    python -m backend.app

This is what the macOS app bundle launches (see scripts/build-app.sh). When
the app quits, the launcher powers the agent VMs off; their disks, and
therefore their state, are kept.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

from backend.config import Settings
from backend.instance import is_running
from backend.logging_config import get_logger
from backend.main import create_app
from backend.vm.ports import LOOPBACK

log = get_logger("app")

APP_NAME = "Agent Office"
EXIT_ALREADY_RUNNING = 3
PREFERRED_PORT = 47600
UI_DIR_ENV_VAR = "AGENT_OFFICE_UI_DIR"
ICON_ENV_VAR = "AGENT_OFFICE_ICON"
DEFAULT_UI_DIR = Path(__file__).resolve().parents[1] / "frontend" / "dist"
# Finder starts apps with a bare PATH; QEMU usually lives in Homebrew's.
EXTRA_PATH_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def extend_path() -> None:
    current = os.environ.get("PATH", "").split(os.pathsep)
    missing = [directory for directory in EXTRA_PATH_DIRS if directory not in current]
    os.environ["PATH"] = os.pathsep.join([*missing, *current])


def pick_port() -> int:
    for port in (PREFERRED_PORT, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((LOOPBACK, port))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise RuntimeError("no free local port")


def brand_process() -> None:
    """Show the app's own name and icon in the Dock instead of Python's."""
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle

        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = APP_NAME
        icon_path = os.environ.get(ICON_ENV_VAR)
        if icon_path and Path(icon_path).is_file():
            icon = NSImage.alloc().initWithContentsOfFile_(icon_path)
            NSApplication.sharedApplication().setApplicationIconImage_(icon)
    except Exception:  # noqa: BLE001 - cosmetic only
        log.info("could not set the Dock name and icon")


def on_window_ready(window: Any) -> None:
    """Runs once the window exists: brand the process and record whether the UI came up."""
    brand_process()
    for _ in range(40):
        time.sleep(0.5)
        try:
            if window.evaluate_js("!!document.querySelector('.shell')"):
                log.info("ui loaded")
                return
        except Exception:  # noqa: BLE001 - the page may still be loading
            continue
    log.warning("ui did not load", extra={"hint": "see the window for an error message"})


def main() -> int:
    extend_path()
    ui_dir = Path(os.environ.get(UI_DIR_ENV_VAR) or DEFAULT_UI_DIR)
    if not (ui_dir / "index.html").is_file():
        print(f"The UI has not been built: {ui_dir} (run `npm run build` in frontend/)", file=sys.stderr)
        return 1

    settings = Settings.from_env()
    if is_running(settings):
        print(f"Another backend is already using {settings.home}.", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    app = create_app(settings, ui_dir=ui_dir)
    port = pick_port()
    server = uvicorn.Server(uvicorn.Config(app, host=LOOPBACK, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="backend", daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            print("The backend failed to start.", file=sys.stderr)
            return 1
        time.sleep(0.05)
    log.info("app window opening", extra={"port": port})

    import webview  # imported late: it needs a GUI session, which tests do not have

    # The token rides in the URL fragment, which is never sent to the server or logged.
    url = f"http://{LOOPBACK}:{port}/#token={app.state.api_token}"
    window = webview.create_window(APP_NAME, url, width=1440, height=900, min_size=(900, 600))
    webview.start(on_window_ready, window)

    # Not reached when the app is quit with Cmd-Q, which ends the process inside
    # the GUI loop. The launcher therefore powers the VMs off afterwards in a
    # separate step (backend/shutdown.py) rather than relying on this code.
    log.info("app window closed")
    server.should_exit = True
    thread.join(timeout=10)
    return 0

if __name__ == "__main__":
    sys.exit(main())
