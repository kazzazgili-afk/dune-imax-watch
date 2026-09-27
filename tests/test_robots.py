from __future__ import annotations

import urllib.robotparser

import pytest

from dune_watch.util.robots import RobotsCache

UA = "dune-imax-watch/0.1 (personal use; contact: test@example.org)"

SCIENCE_MUSEUM_ROBOTS = """
User-agent: *
Crawl-Delay: 20
Allow: /core/*.css$
Disallow: /core/
Disallow: /profiles/
Disallow: /admin
Disallow: /search
Disallow: /user
"""

BFI_ROBOTS = """
User-agent: *
Disallow: /Online/seatSelect.asp
Disallow: /Online/shoppingCart.asp
Disallow: /Online/loadArticle.asp
Disallow: /WebAPI/
"""

VUE_ROBOTS = """
User-agent: *
Disallow: /book-tickets/
Disallow: /showing/
Disallow: /screening/
"""


def cache_with(monkeypatch, robots_text: str, status: int = 200) -> RobotsCache:
    """Builds a RobotsCache whose HTTP fetch is stubbed, so tests never touch the network."""
    class FakeResponse:
        status_code = status
        text = robots_text

    cache = RobotsCache(UA)
    monkeypatch.setattr(
        "dune_watch.util.robots.requests.get", lambda *a, **k: FakeResponse()
    )
    return cache


def test_science_museum_content_page_is_allowed(monkeypatch):
    cache = cache_with(monkeypatch, SCIENCE_MUSEUM_ROBOTS)
    verdict = cache.check("https://www.sciencemuseum.org.uk/see-and-do/dune-part-three")
    assert verdict.allowed is True
    assert verdict.crawl_delay_seconds == 20


def test_crawl_delay_is_a_floor_not_a_ceiling(monkeypatch):
    cache = cache_with(monkeypatch, SCIENCE_MUSEUM_ROBOTS)
    url = "https://www.sciencemuseum.org.uk/see-and-do/dune-part-three"
    # A slower configured interval is respected as-is...
    assert cache.effective_interval_seconds(url, 1200) == 1200
    # ...but nothing may poll faster than the published crawl-delay.
    assert cache.effective_interval_seconds(url, 5) == 20
    assert cache.effective_interval_seconds(url, 20) == 20


def test_bfi_transaction_paths_are_disallowed(monkeypatch):
    cache = cache_with(monkeypatch, BFI_ROBOTS)
    for path in ("/Online/seatSelect.asp", "/Online/shoppingCart.asp", "/WebAPI/anything"):
        assert cache.check(f"https://whatson.bfi.org.uk{path}").allowed is False


def test_vue_showing_prefix_rule_does_not_cover_nested_path(monkeypatch):
    """`Disallow: /showing/` is a prefix rule, so it does not match
    /cinema/<x>/showing/<y>. Vue is excluded for being Cloudflare-walled, not by
    robots.txt - worth pinning so the distinction stays honest."""
    cache = cache_with(monkeypatch, VUE_ROBOTS)
    assert cache.check("https://www.myvue.com/showing/dune").allowed is False
    assert cache.check("https://www.myvue.com/book-tickets/summary").allowed is False
    assert cache.check(
        "https://www.myvue.com/cinema/manchester-printworks/showing/dune"
    ).allowed is True


def test_missing_robots_txt_allows_everything(monkeypatch):
    cache = cache_with(monkeypatch, "", status=404)
    verdict = cache.check("https://example.org/anything")
    assert verdict.allowed is True
    assert verdict.crawl_delay_seconds is None


def test_protected_robots_txt_is_treated_as_stay_out(monkeypatch):
    cache = cache_with(monkeypatch, "", status=403)
    assert cache.check("https://example.org/anything").allowed is False


def test_unreachable_robots_txt_allows_but_keeps_configured_interval(monkeypatch):
    import requests

    cache = RobotsCache(UA)

    def boom(*args, **kwargs):
        raise requests.ConnectionError("dns down")

    monkeypatch.setattr("dune_watch.util.robots.requests.get", boom)
    verdict = cache.check("https://example.org/page")
    assert verdict.allowed is True
    assert cache.effective_interval_seconds("https://example.org/page", 1200) == 1200


def test_robots_is_fetched_once_per_host(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = SCIENCE_MUSEUM_ROBOTS

    def counting_get(url, **kwargs):
        calls.append(url)
        return FakeResponse()

    cache = RobotsCache(UA)
    monkeypatch.setattr("dune_watch.util.robots.requests.get", counting_get)
    for _ in range(5):
        cache.check("https://www.sciencemuseum.org.uk/see-and-do/dune-part-three")
    assert len(calls) == 1
