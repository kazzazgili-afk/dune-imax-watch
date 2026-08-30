from __future__ import annotations

import pytest

from dune_watch.adapters.base import AdapterFetchError
from dune_watch.adapters.imap_newsletter import ImapNewsletterAdapter
from dune_watch.config import AppConfig, FilmConfig, NotificationsConfig, PollingConfig, VenueConfig


def make_app_config() -> AppConfig:
    return AppConfig(
        film=FilmConfig(
            title="Dune: Part Three",
            keywords=["dune"],
            format_keywords=["imax 70mm", "70mm", "imax"],
            opening_window_start="2026-12-01",
            opening_window_end="2027-02-28",
        ),
        polling=PollingConfig(),
        venues=[],
        state_db_path=":memory:",
        notifications=NotificationsConfig(channels={}, auto_open_enabled=False, auto_open_min_urgency="HIGH"),
    )


def make_venue(extra=None) -> VenueConfig:
    return VenueConfig(
        id="bfi_imax", name="BFI IMAX", enabled=True, venue_type="imap_newsletter",
        poll_interval_minutes=60,
        extra=extra or {
            "imap": {
                "folder": "INBOX",
                "sender_filter": ["boxoffice@bfi.org.uk"],
                "subject_keywords": ["imax", "dune", "on sale", "tickets"],
                "lookback_days_on_first_run": 30,
            }
        },
    )


class FakeImap:
    def __init__(self, messages: dict[bytes, bytes]):
        self._messages = messages

    def login(self, user, password):
        return "OK", []

    def select(self, folder):
        return "OK", []

    def search(self, charset, criteria):
        return "OK", [b" ".join(self._messages.keys())]

    def fetch(self, uid, spec):
        raw = self._messages.get(uid)
        if raw is None:
            return "NO", [None]
        return "OK", [(b"1 (RFC822 {%d}" % len(raw), raw)]

    def logout(self):
        return "BYE", []


def setup_imap_env(monkeypatch):
    monkeypatch.setenv("DUNE_WATCH_IMAP_HOST", "imap.example.org")
    monkeypatch.setenv("DUNE_WATCH_IMAP_PORT", "993")
    monkeypatch.setenv("DUNE_WATCH_IMAP_USER", "kazzazgili@gmail.com")
    monkeypatch.setenv("DUNE_WATCH_IMAP_PASS", "app-password")


def test_matching_email_produces_listing(mocker, monkeypatch, load_fixture_email):
    setup_imap_env(monkeypatch)
    raw = load_fixture_email("bfi_onsale_announcement.eml")
    fake = FakeImap({b"1": raw})
    mocker.patch("imaplib.IMAP4_SSL", return_value=fake)

    adapter = ImapNewsletterAdapter(make_venue(), make_app_config())
    listings = adapter.fetch()

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source_type == "imap_newsletter"
    assert listing.source_message_id == "<onsale-2026-08-30@bfi.org.uk>"
    assert listing.availability == "bookable"
    assert listing.booking_link is not None
    assert "bfi.org.uk" in listing.booking_link
    assert listing.format_label is not None
    assert listing.show_date == "2026-12-19"


def test_non_matching_newsletter_is_filtered_out(mocker, monkeypatch, load_fixture_email):
    setup_imap_env(monkeypatch)
    raw = load_fixture_email("bfi_general_newsletter_no_match.eml")
    fake = FakeImap({b"1": raw})
    mocker.patch("imaplib.IMAP4_SSL", return_value=fake)

    adapter = ImapNewsletterAdapter(make_venue(), make_app_config())
    listings = adapter.fetch()
    assert listings == []


def test_html_multipart_body_extracts_link_and_text(mocker, monkeypatch, load_fixture_email):
    setup_imap_env(monkeypatch)
    raw = load_fixture_email("bfi_html_multipart.eml")
    fake = FakeImap({b"1": raw})
    mocker.patch("imaplib.IMAP4_SSL", return_value=fake)

    adapter = ImapNewsletterAdapter(make_venue(), make_app_config())
    listings = adapter.fetch()
    assert len(listings) == 1
    assert listings[0].booking_link is not None
    assert "whatson.bfi.org.uk" in listings[0].booking_link


def test_missing_date_in_body_handled_gracefully(mocker, monkeypatch, load_fixture_email):
    setup_imap_env(monkeypatch)
    raw = load_fixture_email("bfi_html_multipart.eml")
    fake = FakeImap({b"1": raw})
    mocker.patch("imaplib.IMAP4_SSL", return_value=fake)

    adapter = ImapNewsletterAdapter(make_venue(), make_app_config())
    listings = adapter.fetch()
    assert listings[0].show_date is None  # this fixture has no explicit date in its body


def test_sender_filter_excludes_other_senders(mocker, monkeypatch, load_fixture_email):
    setup_imap_env(monkeypatch)
    raw = load_fixture_email("bfi_onsale_announcement.eml")
    fake = FakeImap({b"1": raw})
    mocker.patch("imaplib.IMAP4_SSL", return_value=fake)

    venue = make_venue({"imap": {
        "folder": "INBOX",
        "sender_filter": ["someone-else@example.org"],
        "subject_keywords": ["imax"],
        "lookback_days_on_first_run": 30,
    }})
    adapter = ImapNewsletterAdapter(venue, make_app_config())
    listings = adapter.fetch()
    assert listings == []


def test_missing_credentials_raises(monkeypatch):
    for var in ("DUNE_WATCH_IMAP_HOST", "DUNE_WATCH_IMAP_USER", "DUNE_WATCH_IMAP_PASS"):
        monkeypatch.delenv(var, raising=False)
    adapter = ImapNewsletterAdapter(make_venue(), make_app_config())
    with pytest.raises(AdapterFetchError):
        adapter.fetch()
