from __future__ import annotations

from unittest.mock import Mock

import pytest

from dune_watch.adapters.base import AdapterFetchError
from dune_watch.adapters.bluesky_feed import BlueskyFeedAdapter
from dune_watch.config import AppConfig, FilmConfig, NotificationsConfig, PollingConfig, VenueConfig

SCIENCE_MUSEUM_DID = "did:plc:vfz6n4by6ayu2ybwpq6qemik"


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


def make_venue(actor: str = SCIENCE_MUSEUM_DID, **bluesky) -> VenueConfig:
    conf = {"actor": actor}
    conf.update(bluesky)
    return VenueConfig(
        id="science_museum_bluesky", name="Science Museum (Bluesky)", enabled=True,
        venue_type="bluesky_feed", poll_interval_minutes=15, extra={"bluesky": conf},
    )


def adapter_returning(payload, mocker, venue=None) -> BlueskyFeedAdapter:
    adapter = BlueskyFeedAdapter(venue or make_venue(), make_app_config())
    response = Mock()
    response.json = Mock(return_value=payload)
    response.raise_for_status = Mock()
    session = Mock()
    session.get = Mock(return_value=response)
    mocker.patch("dune_watch.adapters.bluesky_feed.build_session", return_value=session)
    return adapter


def test_real_venue_feed_produces_no_false_positives(mocker, load_fixture_json):
    """The venue's actual recent feed (captured 2026-09-28) is about eclipses, ice cream
    and The Odyssey - nothing about Dune. It must stay silent."""
    payload = load_fixture_json("bluesky_science_museum_real_feed.json")
    assert adapter_returning(payload, mocker).fetch() == []


def test_dune_on_sale_post_is_detected_as_bookable(mocker, load_fixture_json):
    """Modelled on the venue's own real on-sale wording, with the film swapped."""
    payload = load_fixture_json("bluesky_science_museum_dune_onsale.json")
    listings = adapter_returning(payload, mocker).fetch()

    assert len(listings) == 1
    listing = listings[0]
    assert listing.availability == "bookable"
    assert listing.format_label is not None
    assert listing.source_type == "bluesky_feed"
    # Prefers the link the venue put in the post over the post permalink.
    assert "my.sciencemuseum.org.uk/events" in listing.booking_link
    assert listing.listing_key().startswith("science_museum_bluesky|post|at://")


@pytest.mark.parametrize("text,expected", [
    # The venue wrote both spellings within three consecutive posts about one film.
    ("New screenings of Dune: Part Three in magnificent IMAX 70mm are now on sale.", "bookable"),
    ("New screenings of Dune: Part Three in glorious IMAX 70 mm are now on sale.", "bookable"),
    ("Register your interest for Dune: Part Three in IMAX 70mm.", "register_interest"),
    ("Dune: Part Three is coming to our IMAX 70mm screen this December.", "unknown"),
])
def test_format_spelling_variants_and_availability(text, expected, mocker):
    payload = {"feed": [{"post": {
        "uri": "at://did:plc:test/app.bsky.feed.post/abc",
        "record": {"text": text},
        "author": {"handle": "sciencemuseum.org.uk"},
    }}]}
    listings = adapter_returning(payload, mocker).fetch()
    assert len(listings) == 1
    assert listings[0].availability == expected


def test_post_without_format_mention_is_ignored(mocker):
    """A general marketing post about the film is not an on-sale signal."""
    payload = {"feed": [{"post": {
        "uri": "at://did:plc:test/app.bsky.feed.post/abc",
        "record": {"text": "Dune: Part Three is out this December. Who's excited?"},
        "author": {"handle": "sciencemuseum.org.uk"},
    }}]}
    assert adapter_returning(payload, mocker).fetch() == []


def test_permalink_is_built_when_post_has_no_embedded_link(mocker):
    payload = {"feed": [{"post": {
        "uri": "at://did:plc:test/app.bsky.feed.post/xyz789",
        "record": {"text": "Dune: Part Three in IMAX 70mm tickets are now on sale!"},
        "author": {"handle": "sciencemuseum.org.uk"},
    }}]}
    listing = adapter_returning(payload, mocker).fetch()[0]
    assert listing.booking_link == "https://bsky.app/profile/sciencemuseum.org.uk/post/xyz789"


def test_missing_actor_is_a_config_error():
    venue = VenueConfig(
        id="x", name="x", enabled=True, venue_type="bluesky_feed",
        poll_interval_minutes=15, extra={"bluesky": {}},
    )
    with pytest.raises(AdapterFetchError, match="bluesky.actor"):
        BlueskyFeedAdapter(venue, make_app_config()).fetch()


def test_unexpected_payload_shape_raises(mocker):
    with pytest.raises(AdapterFetchError, match="Unexpected Bluesky response"):
        adapter_returning({"nope": True}, mocker).fetch()


def test_malformed_post_is_skipped_not_fatal(mocker, load_fixture_json):
    payload = load_fixture_json("bluesky_science_museum_dune_onsale.json")
    payload["feed"].insert(0, {"post": None})
    assert len(adapter_returning(payload, mocker).fetch()) == 1
