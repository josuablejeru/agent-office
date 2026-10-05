"""Web search parsing, bot-check detection and re-finding page elements."""

from __future__ import annotations

from pathlib import Path

import pytest

from guest.browser import shape_snapshot
from guest.elements import find_again, same_page
from guest.search import detect_challenge, parse_results, search_url, unwrap_result_url

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("engine", ["duckduckgo", "bing"])
def test_results_are_parsed_from_saved_pages(engine: str) -> None:
    name = {"duckduckgo": "search_ddg.html", "bing": "search_bing.html"}[engine]
    results = parse_results(engine, (FIXTURES / name).read_text())
    assert 5 <= len(results) <= 8
    for result in results:
        assert result["title"] and result["url"].startswith("http")
        assert "duckduckgo.com" not in result["url"] and "bing.com/ck/" not in result["url"]
    assert any("python" in result["url"] for result in results)
    assert sum(1 for result in results if result["snippet"]) >= 3
    assert len({result["url"] for result in results}) == len(results)


def test_redirect_wrappers_are_removed() -> None:
    assert unwrap_result_url(
        "//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html&rut=abc"
    ) == "https://docs.python.org/3/library/asyncio.html"
    assert unwrap_result_url(
        "https://www.bing.com/ck/a?!&&p=x&u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9hP2I9MQ&ntb=1"
    ) == "https://example.com/a?b=1"
    assert unwrap_result_url("https://example.org/page") == "https://example.org/page"
    assert unwrap_result_url("https://www.bing.com/ck/a?u=a1%%%") .startswith("https://www.bing.com")
    assert search_url("duckduckgo", "a b&c") == "https://html.duckduckgo.com/html/?q=a+b%26c"


def test_empty_or_unrelated_pages_give_no_results() -> None:
    assert parse_results("duckduckgo", "<html><body>No results.</body></html>") == []
    assert parse_results("bing", "") == []


@pytest.mark.parametrize(
    ("url", "title", "text"),
    [
        ("https://www.google.com/sorry/index?continue=x", "", "Our systems have detected unusual traffic"),
        ("https://shop.example/", "Just a moment...", "Checking"),
        ("https://site.example/x", "Site", "Please verify you are human to continue"),
        ("https://site.example/x", "Attention Required! | Cloudflare", ""),
    ],
)
def test_bot_checks_are_recognised(url: str, title: str, text: str) -> None:
    assert detect_challenge(url, title, text)
    snapshot = shape_snapshot({"text": text, "elements": [{"ref": 1}]}, url, title, 1000)
    assert snapshot["blocked"] is True and snapshot["elements"] == [] and "web_search" in snapshot["note"]


def test_ordinary_pages_are_not_flagged() -> None:
    assert detect_challenge("https://example.com/", "Example Domain", "This domain is for use in examples") is None
    assert detect_challenge("https://news.example/captcha-history", "A history of the CAPTCHA", "Long article " * 50) is None
    snapshot = shape_snapshot({"text": "hello", "elements": [{"ref": 1}]}, "https://example.com/", "Example", 1000)
    assert "blocked" not in snapshot and snapshot["elements"] == [{"ref": 1}]


def element(ref: int, tag: str = "a", label: str = "", href: str | None = None, kind: str | None = None) -> dict:
    data: dict = {"ref": ref, "tag": tag, "label": label}
    if href:
        data["href"] = href
    if kind:
        data["type"] = kind
    return data


def test_element_is_found_again_after_renumbering() -> None:
    wanted = element(12, label="Contact us", href="https://x.example/contact")
    fresh = [
        element(1, label="Home", href="https://x.example/"),
        element(2, label="Advert", href="https://ads.example/"),  # new element shifted the numbers
        element(3, label="Contact us", href="https://x.example/contact"),
    ]
    assert find_again(wanted, fresh) == 3
    # same address, changed label: still unambiguous by address
    assert find_again(wanted, [element(5, label="Contact", href="https://x.example/contact")]) == 5
    # buttons have no address: matched by tag and label
    assert find_again(element(4, tag="button", label="Search"), [element(9, tag="button", label="Search")]) == 9


def test_ambiguous_or_missing_elements_are_not_guessed() -> None:
    wanted = element(2, tag="button", label="Buy")
    assert find_again(wanted, [element(1, tag="button", label="Buy"), element(2, tag="button", label="Buy")]) is None
    assert find_again(wanted, [element(1, tag="a", label="Buy", href="https://x/")]) is None
    assert find_again(element(1, tag="div"), [element(1, tag="div")]) is None  # nothing to go by
    assert find_again(wanted, []) is None


def test_same_page_ignores_fragments() -> None:
    assert same_page("https://x.example/a#top", "https://x.example/a#results")
    assert not same_page("https://x.example/a", "https://x.example/b")
