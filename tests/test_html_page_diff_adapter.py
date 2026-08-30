from __future__ import annotations

from unittest.mock import Mock

from dune_watch.adapters.html_page_diff import HtmlPageDiffAdapter
from dune_watch.config import AppConfig, FilmConfig, NotificationsConfig, PollingConfig, VenueConfig


def make_app_config() -> AppConfig:
    return AppConfig(
        film=FilmConfig(
            title="Dune: Part Three",
            keywords=["dune", "dune: part three"],
            format_keywords=["imax 70mm", "70mm", "imax"],
            opening_window_start="2026-12-01",
            opening_window_end="2027-02-28",
        ),
        polling=PollingConfig(),
        venues=[],
        state_db_path=":memory:",
        notifications=NotificationsConfig(channels={}, auto_open_enabled=False, auto_open_min_urgency="HIGH"),
    )


def make_venue(extra: dict) -> VenueConfig:
    return VenueConfig(
        id="science_museum_imax", name="Science Museum IMAX", enabled=True,
        venue_type="html_page_diff", poll_interval_minutes=20, extra=extra,
    )


def _adapter_with_response(html: str, mocker, parser_hints=None) -> HtmlPageDiffAdapter:
    venue = make_venue({
        "url": "https://www.sciencemuseum.org.uk/see-and-do/dune-part-three",
        "parser_hints": parser_hints or {
            "register_interest_markers": ["register your interest", "register interest"],
            "showtime_container_selector": ".showtime",
        },
    })
    adapter = HtmlPageDiffAdapter(venue, make_app_config())

    mock_response = Mock()
    mock_response.text = html
    mock_response.raise_for_status = Mock()
    mock_session = Mock()
    mock_session.get.return_value = mock_response
    mocker.patch("dune_watch.adapters.html_page_diff.build_session", return_value=mock_session)
    return adapter


def test_register_interest_page_returns_placeholder(mocker, load_fixture_html):
    html = load_fixture_html("science_museum_register_interest.html")
    adapter = _adapter_with_response(html, mocker)
    listings = adapter.fetch()
    assert len(listings) == 1
    assert listings[0].availability == "register_interest"
    assert listings[0].show_date is None


def test_live_showtimes_parsed_correctly(mocker, load_fixture_html):
    html = load_fixture_html("science_museum_showtimes_live.html")
    adapter = _adapter_with_response(html, mocker)
    listings = adapter.fetch()
    assert len(listings) == 3

    dates = {listing.show_date for listing in listings}
    assert "2026-12-19" in dates
    assert "2026-12-20" in dates
    assert "2026-12-21" in dates

    bookable = [listing for listing in listings if listing.availability == "bookable"]
    sold_out = [listing for listing in listings if listing.availability == "sold_out"]
    assert len(bookable) == 2
    assert len(sold_out) == 1
    for listing in bookable:
        assert listing.booking_link is not None
        assert listing.format_label is not None


def test_malformed_page_degrades_gracefully(mocker, load_fixture_html):
    html = load_fixture_html("science_museum_malformed.html")
    adapter = _adapter_with_response(html, mocker)
    listings = adapter.fetch()
    assert len(listings) == 3
    unavailable = [listing for listing in listings if listing.availability == "unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0].show_date is None


def test_exception_during_one_element_parse_is_skipped(mocker, load_fixture_html):
    html = load_fixture_html("science_museum_showtimes_live.html")
    adapter = _adapter_with_response(html, mocker)
    original = adapter._parse_showtime_element
    call_count = {"n": 0}

    def flaky(element, page_format, fingerprint):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise ValueError("simulated parse failure")
        return original(element, page_format, fingerprint)

    mocker.patch.object(adapter, "_parse_showtime_element", side_effect=flaky)
    listings = adapter.fetch()
    assert len(listings) == 2  # one of three skipped, no crash


def test_page_without_film_keyword_returns_empty(mocker):
    html = "<html><body><p>Some other film entirely.</p></body></html>"
    adapter = _adapter_with_response(html, mocker)
    listings = adapter.fetch()
    assert listings == []


def test_fingerprint_is_stable_for_identical_html(mocker, load_fixture_html):
    html = load_fixture_html("science_museum_register_interest.html")
    adapter1 = _adapter_with_response(html, mocker)
    listings1 = adapter1.fetch()
    adapter2 = _adapter_with_response(html, mocker)
    listings2 = adapter2.fetch()
    assert listings1[0].raw_fingerprint == listings2[0].raw_fingerprint
