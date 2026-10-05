"""Web search results parsed from a search engine's result page (stdlib only).

The agent's own browser loads the page; these functions turn its HTML into a
short list of results, so the agent does not have to drive a search engine by
hand. Parsing is separate from browsing so it can be tested on saved pages.
"""

from __future__ import annotations

import base64
import binascii
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlsplit

MAX_RESULTS = 8
MAX_SNIPPET_CHARS = 300

# Tried in order. Each needs a parser below.
ENGINES: dict[str, str] = {
    "duckduckgo": "https://html.duckduckgo.com/html/?q={query}",
    "bing": "https://www.bing.com/search?q={query}",
}
ENGINE_DOMAINS = ("duckduckgo.com", "bing.com", "microsoft.com/bing")

# Kept narrow: an article about CAPTCHAs must not be mistaken for one.
CHALLENGE_URL_MARKS = ("google.com/sorry/", "/cdn-cgi/challenge-platform")
CHALLENGE_TEXT_MARKS = (
    "unusual traffic",
    "verify you are human",
    "verify you are a human",
    "are you a robot",
    "not a robot",
    "complete the captcha",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
)
CHALLENGE_TITLES = ("just a moment", "attention required", "access denied", "are you a robot")


def search_url(engine: str, query: str) -> str:
    return ENGINES[engine].format(query=quote_plus(query))


def detect_challenge(url: str, title: str, text: str) -> str | None:
    """Return a plain explanation if the page is a bot check instead of content."""
    lowered_url, lowered_title, lowered_text = url.lower(), title.lower(), text[:3000].lower()
    blocked = (
        any(mark in lowered_url for mark in CHALLENGE_URL_MARKS)
        or any(lowered_title.startswith(mark) for mark in CHALLENGE_TITLES)
        or any(mark in lowered_text for mark in CHALLENGE_TEXT_MARKS)
    )
    if not blocked:
        return None
    return (
        "This site is showing a bot check (CAPTCHA) instead of its content. Do not try to get "
        "past it. Use web_search to find the information elsewhere, or tell the user they can "
        "take control of the computer to pass the check."
    )


def unwrap_result_url(href: str) -> str:
    """Search engines wrap result links in their own redirects; return the real address."""
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    query = parse_qs(parts.query)
    if parts.netloc.endswith("duckduckgo.com") and parts.path.startswith("/l/") and "uddg" in query:
        return query["uddg"][0]
    if parts.netloc.endswith("bing.com") and parts.path.startswith("/ck/") and "u" in query:
        encoded = query["u"][0]
        if encoded.startswith("a1"):
            payload = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
            try:
                decoded = base64.urlsafe_b64decode(payload).decode()
            except (binascii.Error, UnicodeDecodeError):
                return href
            return decoded if decoded.startswith("http") else href
    return href


class _ResultParser(HTMLParser):
    """Collects (title, href, snippet) triples; subclasses say where each one starts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._capture: str | None = None  # "title" or "snippet"
        self._capture_tag = ""
        self._depth = 0
        self._buffer: list[str] = []

    def begin(self, kind: str, tag: str) -> None:
        self._capture, self._capture_tag, self._depth, self._buffer = kind, tag, 1, []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._capture or tag != self._capture_tag:
            return
        self._depth -= 1
        if self._depth == 0:
            text = " ".join("".join(self._buffer).split())
            if self.results:
                self.results[-1][self._capture] = text
            self._capture = None

    def nested(self, tag: str) -> None:
        if self._capture and tag == self._capture_tag:
            self._depth += 1


class _DuckDuckGoParser(_ResultParser):
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self.results.append({"href": dict(attrs).get("href") or "", "title": "", "snippet": ""})
            self.begin("title", tag)
        elif "result__snippet" in classes and self.results:
            self.begin("snippet", tag)
        else:
            self.nested(tag)


class _BingParser(_ResultParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_result = False
        self._in_heading = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if tag == "li" and "b_algo" in classes:
            self._in_result = True
            self.results.append({"href": "", "title": "", "snippet": ""})
        elif self._in_result and tag == "h2":
            self._in_heading = True
        elif self._in_heading and tag == "a" and not self.results[-1]["href"]:
            self.results[-1]["href"] = dict(attrs).get("href") or ""
            self.begin("title", tag)
        elif self._in_result and tag == "p" and not self.results[-1]["snippet"] and not self._capture:
            self.begin("snippet", tag)
        else:
            self.nested(tag)

    def handle_endtag(self, tag: str) -> None:
        super().handle_endtag(tag)
        if tag == "h2":
            self._in_heading = False


PARSERS: dict[str, type[_ResultParser]] = {"duckduckgo": _DuckDuckGoParser, "bing": _BingParser}


def parse_results(engine: str, html: str) -> list[dict[str, Any]]:
    """Results of one engine's page: title, real address and snippet, engine links removed."""
    parser = PARSERS[engine]()
    parser.feed(html)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in parser.results:
        url = unwrap_result_url(raw["href"])
        host = urlsplit(url).netloc
        if not url.startswith("http") or not raw["title"] or url in seen:
            continue
        if any(host == domain or host.endswith("." + domain) for domain in ENGINE_DOMAINS):
            continue  # the engine's own pages and ad redirects
        seen.add(url)
        results.append(
            {"title": raw["title"], "url": url, "snippet": raw["snippet"][:MAX_SNIPPET_CHARS]}
        )
        if len(results) >= MAX_RESULTS:
            break
    return results
