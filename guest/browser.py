"""Browser operations: drive the Chrome window on the agent's desktop.

Chrome runs as a normal, visible window in the desktop session with one
persistent profile, so cookies, logins and storage survive restarts and the
user sees (and can take over) exactly the browser the agent uses. The daemon
attaches to it over the DevTools protocol, which listens on guest loopback only.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import urllib.request
from pathlib import Path
from typing import Any

from guest.elements import find_again, same_page
from guest.search import ENGINES, detect_challenge, parse_results, search_url
from guest.validation import OperationError, optional_bool, optional_number, optional_str, require_str

CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
PROFILE_DIR = Path.home() / ".config" / "agent-office-browser"
BROWSER_BINARIES = ("google-chrome", "chromium", "chromium-browser")
BROWSER_FLAGS = (
    f"--remote-debugging-port={CDP_PORT}",
    "--remote-debugging-address=127.0.0.1",
    # Without this, Chrome asks to unlock a keyring that an autologin session never opened.
    "--password-store=basic",
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-crash-restore-bubble",
    "--start-maximized",
)
STARTUP_TIMEOUT_SECONDS = 45.0
NAVIGATION_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 10_000
SETTLE_TIMEOUT_MS = 5_000

ALLOWED_SCHEMES = ("http://", "https://", "about:")
MAX_URL_LENGTH = 4096
MAX_TYPE_LENGTH = 20_000
# A snapshot follows every action; `browser.extract_text` returns the long form.
BRIEF_TEXT_CHARS = 2_500
BRIEF_ELEMENTS = 40
FULL_TEXT_CHARS = 10_000
FULL_ELEMENTS = 120
SCREENSHOT_JPEG_QUALITY = 70

# Numbers the page's visible interactive elements so a model can refer to them
# without writing CSS selectors.
SNAPSHOT_SCRIPT = """
(limit) => {
  const selector = 'a[href], button, input, textarea, select, summary, ' +
    '[role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox], ' +
    '[contenteditable=""], [contenteditable="true"]';
  document.querySelectorAll('[data-gl-ref]').forEach((el) => el.removeAttribute('data-gl-ref'));
  const elements = [];
  for (const el of document.querySelectorAll(selector)) {
    if (elements.length >= limit) break;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (rect.width < 2 || rect.height < 2 || style.visibility === 'hidden' || el.disabled) continue;
    if (el.tagName === 'INPUT' && el.type === 'hidden') continue;
    const ref = elements.length + 1;
    el.setAttribute('data-gl-ref', String(ref));
    const label = (el.innerText || el.value || el.getAttribute('aria-label') ||
      el.getAttribute('placeholder') || el.getAttribute('title') || el.name || '')
      .replace(/\\s+/g, ' ').trim().slice(0, 80);
    const item = { ref, tag: el.tagName.toLowerCase(), label };
    if (el.tagName === 'INPUT') item.type = el.type;
    if (el.tagName === 'A') item.href = el.href.slice(0, 200);
    elements.push(item);
  }
  return { text: document.body ? document.body.innerText : '', elements };
}
"""


def validate_url(url: str) -> str:
    if not url.startswith(ALLOWED_SCHEMES):
        if "://" in url or url.startswith(("javascript:", "data:", "file:")):
            raise OperationError("only http and https URLs can be opened")
        url = "https://" + url
    return url


def shape_snapshot(raw: dict[str, Any], url: str, title: str, text_chars: int) -> dict[str, Any]:
    """Trim a page snapshot to a size a model's context can afford."""
    text = "\n".join(line.strip() for line in str(raw.get("text", "")).splitlines() if line.strip())
    snapshot: dict[str, Any] = {
        "url": url,
        "title": title,
        "text": text[:text_chars],
        "text_truncated": len(text) > text_chars,
        "elements": raw.get("elements", []),
    }
    if blocked := detect_challenge(url, title, text):
        # Say so in words: a model otherwise reads the bot check as the page's content.
        snapshot = {"blocked": True, "note": blocked, **snapshot, "elements": []}
    return snapshot


def find_browser() -> str:
    for name in BROWSER_BINARIES:
        if path := shutil.which(name):
            return path
    raise OperationError("no Chrome or Chromium is installed on this computer")


def cdp_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2):
            return True
    except OSError:
        return False


class StalePage(OperationError):
    """The element asked for is not on the page (any more)."""


class Browser:
    """One attached browser per daemon. Serialises operations: there is a single page focus."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        # The elements of the last snapshot by number, and the page they were on.
        self._elements: dict[int, dict[str, Any]] = {}
        self._elements_url = ""
        # Set once the computer is shutting down, so the window is not reopened.
        self._closing = False

    async def ensure_started(self) -> None:
        """Open the browser window on the desktop if it is not already there."""
        if await asyncio.to_thread(cdp_alive):
            return
        display = os.environ.get("DISPLAY", "")
        if not display or not Path(f"/tmp/.X11-unix/X{display.rsplit(':', 1)[-1]}").exists():
            raise OperationError("the desktop session is not running yet")
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        # A stale lock from an unclean shutdown would make Chrome refuse the profile.
        for stale in PROFILE_DIR.glob("Singleton*"):
            stale.unlink(missing_ok=True)
        env = dict(os.environ)
        session_bus = Path(f"/run/user/{os.getuid()}/bus")
        if session_bus.exists():
            env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={session_bus}")
        process = await asyncio.create_subprocess_exec(
            find_browser(), f"--user-data-dir={PROFILE_DIR}", *BROWSER_FLAGS, "about:blank",
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT_SECONDS
        while not await asyncio.to_thread(cdp_alive):
            if process.returncode is not None:
                raise OperationError("the browser exited while starting")
            if asyncio.get_running_loop().time() > deadline:
                raise OperationError("the browser did not start; is the desktop session running?")
            await asyncio.sleep(0.5)

    async def _connect(self) -> Any:
        from playwright.async_api import async_playwright

        await self.ensure_started()
        if self._browser is None or not self._browser.is_connected():
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(CDP_URL)
            self._page = None
        context = self._browser.contexts[0]
        if self._page is None or self._page.is_closed():
            pages = [page for page in context.pages if not page.is_closed()]
            self._page = pages[-1] if pages else await context.new_page()
            self._page.set_default_timeout(ACTION_TIMEOUT_MS)
        return self._page

    async def _run(self, action: Any) -> dict[str, Any]:
        from playwright.async_api import Error as PlaywrightError

        async with self._lock:
            try:
                return await action(await self._connect())
            except PlaywrightError as exc:
                # First line only: Playwright appends a long call log.
                raise OperationError(str(exc).splitlines()[0][:300]) from exc

    async def _snapshot(self, page: Any, text_chars: int, max_elements: int) -> dict[str, Any]:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=SETTLE_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - a slow page still gets a best-effort snapshot
            pass
        raw = await page.evaluate(SNAPSHOT_SCRIPT, max_elements)
        self._elements = {int(element["ref"]): element for element in raw.get("elements", [])}
        self._elements_url = page.url
        return shape_snapshot(raw, page.url, await page.title(), text_chars)

    async def _target(self, page: Any, args: dict[str, Any]) -> Any:
        ref = args.get("ref")
        selector = optional_str(args, "selector", 500)
        if ref is None:
            if not selector:
                raise OperationError("give the element's 'ref' number (or a CSS 'selector')")
            locator = page.locator(selector)
            if await locator.count() == 0:
                raise StalePage(f"nothing on the current page matches {selector!r}")
            return locator.first
        if isinstance(ref, bool) or not isinstance(ref, (int, str)) or not str(ref).isdigit():
            raise OperationError("'ref' must be an element number from the page snapshot")
        number = int(ref)
        locator = page.locator(f'[data-gl-ref="{number}"]')
        if await locator.count() > 0:
            return locator.first
        # The page has redrawn and lost its numbering. Renumber it, and look for
        # the element this number described.
        wanted = self._elements.get(number)
        came_from = self._elements_url
        raw = await page.evaluate(SNAPSHOT_SCRIPT, FULL_ELEMENTS)
        self._elements = {int(element["ref"]): element for element in raw.get("elements", [])}
        self._elements_url = page.url
        if wanted is not None and same_page(came_from, page.url):
            found = find_again(wanted, list(self._elements.values()))
            if found is not None:
                return page.locator(f'[data-gl-ref="{found}"]').first
        raise StalePage(
            f"element {number} is no longer on the page: the page has changed. "
            "The current elements are listed here; pick from these."
        )

    async def _act(self, page: Any, action: Any) -> dict[str, Any]:
        """Run an action on an element; if the page changed under it, return the fresh page."""
        try:
            await action()
        except StalePage as exc:
            snapshot = await self._snapshot(page, BRIEF_TEXT_CHARS, BRIEF_ELEMENTS)
            return {"error": str(exc), **snapshot}
        return await self._snapshot(page, BRIEF_TEXT_CHARS, BRIEF_ELEMENTS)

    async def goto(self, args: dict[str, Any]) -> dict[str, Any]:
        url = validate_url(require_str(args, "url", MAX_URL_LENGTH))

        async def action(page: Any) -> dict[str, Any]:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            snapshot = await self._snapshot(page, BRIEF_TEXT_CHARS, BRIEF_ELEMENTS)
            return {"status": response.status if response else None, **snapshot}

        return await self._run(action)

    async def click(self, args: dict[str, Any]) -> dict[str, Any]:
        async def action(page: Any) -> dict[str, Any]:
            context = page.context
            known = len(context.pages)

            async def click() -> None:
                await (await self._target(page, args)).click()
                await page.wait_for_timeout(600)

            result = await self._act(page, click)
            if "error" not in result and len(context.pages) > known:
                # The click opened a new tab: follow it.
                self._page = page = context.pages[-1]
                page.set_default_timeout(ACTION_TIMEOUT_MS)
                result = await self._snapshot(page, BRIEF_TEXT_CHARS, BRIEF_ELEMENTS)
            return result

        return await self._run(action)

    async def type(self, args: dict[str, Any]) -> dict[str, Any]:
        text = args.get("text")
        if not isinstance(text, str) or len(text) > MAX_TYPE_LENGTH:
            raise OperationError(f"'text' must be a string of at most {MAX_TYPE_LENGTH} characters")
        submit = optional_bool(args, "submit", False)

        async def action(page: Any) -> dict[str, Any]:
            async def fill() -> None:
                target = await self._target(page, args)
                await target.fill(text)
                if submit:
                    await target.press("Enter")
                    await page.wait_for_timeout(800)

            return await self._act(page, fill)

        return await self._run(action)

    async def search(self, args: dict[str, Any]) -> dict[str, Any]:
        """Search the web and return a short list of results."""
        query = require_str(args, "query", 300).strip()
        if not query:
            raise OperationError("'query' must not be empty")

        async def action(page: Any) -> dict[str, Any]:
            problems: list[str] = []
            for engine in ENGINES:
                try:
                    await page.goto(
                        search_url(engine, query), wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS
                    )
                    await page.wait_for_timeout(800)
                    html = await page.content()
                    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                except Exception as exc:  # noqa: BLE001 - try the next engine
                    problems.append(f"{engine}: {str(exc).splitlines()[0][:120]}")
                    continue
                if detect_challenge(page.url, await page.title(), text):
                    problems.append(f"{engine}: showed a bot check")
                    continue
                results = parse_results(engine, html)
                if results:
                    return {"query": query, "engine": engine, "results": results}
                problems.append(f"{engine}: no results")
            raise OperationError("the web search did not work (" + "; ".join(problems) + ")")

        return await self._run(action)

    async def press(self, args: dict[str, Any]) -> dict[str, Any]:
        key = require_str(args, "key", 40)

        async def action(page: Any) -> dict[str, Any]:
            await page.keyboard.press(key)
            await page.wait_for_timeout(500)
            return await self._snapshot(page, BRIEF_TEXT_CHARS, BRIEF_ELEMENTS)

        return await self._run(action)

    async def scroll(self, args: dict[str, Any]) -> dict[str, Any]:
        direction = args.get("direction", "down")
        if direction not in ("up", "down"):
            raise OperationError("'direction' must be 'up' or 'down'")
        pages = optional_number(args, "pages", 1, 0.1, 20)

        async def action(page: Any) -> dict[str, Any]:
            sign = 1 if direction == "down" else -1
            await page.evaluate("(f) => window.scrollBy(0, f * window.innerHeight)", sign * pages)
            await page.wait_for_timeout(300)
            position = await page.evaluate(
                "() => ({ y: Math.round(scrollY), height: document.documentElement.scrollHeight })"
            )
            return {"url": page.url, "scroll_y": position["y"], "page_height": position["height"]}

        return await self._run(action)

    async def extract_text(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self._run(lambda page: self._snapshot(page, FULL_TEXT_CHARS, FULL_ELEMENTS))

    async def screenshot(self, args: dict[str, Any]) -> dict[str, Any]:
        async def action(page: Any) -> dict[str, Any]:
            image = await page.screenshot(type="jpeg", quality=SCREENSHOT_JPEG_QUALITY)
            size = page.viewport_size or await page.evaluate(
                "() => ({ width: innerWidth, height: innerHeight })"
            )
            return {
                "url": page.url,
                "title": await page.title(),
                "width": size["width"],
                "height": size["height"],
                "image_format": "jpeg",
                "image_base64": base64.b64encode(image).decode(),
            }

        return await self._run(action)

    async def current_url(self, args: dict[str, Any]) -> dict[str, Any]:
        async def action(page: Any) -> dict[str, Any]:
            return {"url": page.url, "title": await page.title()}

        return await self._run(action)

    async def close(self, args: dict[str, Any]) -> dict[str, Any]:
        """Quit the browser cleanly so cookies and storage reach the disk.

        Called before the computer shuts down: when the desktop session ends
        first, Chrome dies with it and loses whatever it had not flushed yet.
        """
        self._closing = True
        if not await asyncio.to_thread(cdp_alive):
            return {"closed": False}

        async def action(page: Any) -> dict[str, Any]:
            session = await self._browser.new_browser_cdp_session()
            await session.send("Browser.close")
            return {"closed": True}

        try:
            result = await self._run(action)
        except OperationError:
            result = {"closed": True}  # the connection drops as the browser exits
        for _ in range(40):
            if not await asyncio.to_thread(cdp_alive):
                break
            await asyncio.sleep(0.25)
        self._browser = self._page = None
        return result

    async def keep_open(self) -> None:
        """Keep a browser window on the desktop, reopening it if it gets closed."""
        while True:
            if not self._closing:
                try:
                    await self.ensure_started()
                except (OperationError, OSError):
                    pass  # no desktop yet (still booting) or no browser installed
            await asyncio.sleep(5)
