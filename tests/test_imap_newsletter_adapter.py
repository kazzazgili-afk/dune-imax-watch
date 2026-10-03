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
        self.searches: list[str] = []

    def login(self, user, password):
        return "OK", []

    def select(self, folder):
        return "OK", []

    def search(self, charset, criteria):
        self.searches.append(criteria)
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


def make_science_museum_venue() -> VenueConfig:
    """Mirrors the real config/config.yaml block, including the sender domain that the
    original filter got wrong (sciencemuseum.ac.uk, not .org.uk)."""
    return VenueConfig(
        id="science_museum_imax_email", name="Science Museum IMAX (Email Alert)",
        enabled=True, venue_type="imap_newsletter", poll_interval_minutes=60,
        extra={
            "imap": {
                "folder": "INBOX",
                "sender_filter": ["sciencemuseum.ac.uk", "sciencemuseum.org.uk"],
                "subject_keywords": ["imax", "dune", "on sale", "tickets", "ronson"],
                "lookback_days_on_first_run": 30,
            }
        },
    )


def test_real_science_museum_onsale_email_is_caught(mocker, monkeypatch, load_fixture_email):
    """Regression for the 9 Sept 2026 miss.

    The real on-sale email came from no-reply@sciencemuseum.ac.uk, but the config
    filtered on 'sciencemuseum.org.uk' - a substring test that never matched - so the
    Science Museum's only ticket release to date was silently dropped.
    """
    setup_imap_env(monkeypatch)
    raw = load_fixture_email("science_museum_onsale_2026-09-09.eml")
    fake = FakeImap({b"1": raw})
    mocker.patch("imaplib.IMAP4_SSL", return_value=fake)

    adapter = ImapNewsletterAdapter(make_science_museum_venue(), make_app_config())
    listings = adapter.fetch()

    # The email announced two previews: 15 Dec at 14.30 and at 19.15.
    assert len(listings) == 2
    assert {l.show_time for l in listings} == {"14:30", "19:15"}
    assert {l.show_date for l in listings} == {"2026-12-15"}

    for listing in listings:
        assert listing.availability == "bookable"
        assert listing.booking_link is not None
        # The booking link must be the tracked booking CTA, never a social/footer link.
        assert "wordfly.com/click" in listing.booking_link
        assert listing.format_label is not None

    # Two showtimes from one email must not collapse onto the same state row.
    assert len({l.listing_key() for l in listings}) == 2


def test_playful_subject_line_still_matches_on_body(mocker, monkeypatch, load_fixture_email):
    """BFI's real announcement was subject-lined 'Head back to Arrakis sooner than
    planned' - no keyword in the subject at all. Keywords must match subject + body,
    and the sender filter must be a domain fragment so noreply@news.bfi.org.uk passes.
    """
    setup_imap_env(monkeypatch)
    raw = load_fixture_email("bfi_arrakis_playful_subject.eml")
    fake = FakeImap({b"1": raw})
    mocker.patch("imaplib.IMAP4_SSL", return_value=fake)

    venue = make_venue(extra={
        "imap": {
            "folder": "INBOX",
            "sender_filter": ["bfi.org.uk"],
            "subject_keywords": ["imax", "dune", "on sale", "tickets"],
            "lookback_days_on_first_run": 30,
        }
    })
    listings = ImapNewsletterAdapter(venue, make_app_config()).fetch()

    assert len(listings) == 1
    assert listings[0].availability == "bookable"
    assert "whatson.bfi.org.uk" in listings[0].booking_link


def test_old_sender_filter_would_have_missed_it(mocker, monkeypatch, load_fixture_email):
    """Pins the root cause so it can't regress: the pre-fix filters matched nothing."""
    setup_imap_env(monkeypatch)
    raw = load_fixture_email("science_museum_onsale_2026-09-09.eml")
    mocker.patch("imaplib.IMAP4_SSL", return_value=FakeImap({b"1": raw}))

    broken = VenueConfig(
        id="science_museum_imax_email", name="Science Museum", enabled=True,
        venue_type="imap_newsletter", poll_interval_minutes=60,
        extra={"imap": {"sender_filter": ["sciencemuseum.org.uk"], "subject_keywords": ["dune"]}},
    )
    assert ImapNewsletterAdapter(broken, make_app_config()).fetch() == []


def test_search_narrows_by_sender_on_the_server(mocker, monkeypatch, load_fixture_email):
    """During an on-sale the email venues poll every 60s. Pulling a month of full
    message bodies each time and filtering locally is slow and rude to the mail host, so
    the sender filter has to be pushed into the IMAP SEARCH."""
    setup_imap_env(monkeypatch)
    fake = FakeImap({b"1": load_fixture_email("science_museum_onsale_2026-09-09.eml")})
    mocker.patch("imaplib.IMAP4_SSL", return_value=fake)

    ImapNewsletterAdapter(make_science_museum_venue(), make_app_config()).fetch()

    assert len(fake.searches) == 2, "one search per configured sender"
    assert all("SINCE" in q for q in fake.searches)
    assert any('FROM "sciencemuseum.ac.uk"' in q for q in fake.searches)
    assert any('FROM "sciencemuseum.org.uk"' in q for q in fake.searches)


def test_search_falls_back_to_date_only_without_a_sender_filter(mocker, monkeypatch, load_fixture_email):
    setup_imap_env(monkeypatch)
    fake = FakeImap({b"1": load_fixture_email("bfi_onsale_announcement.eml")})
    mocker.patch("imaplib.IMAP4_SSL", return_value=fake)

    venue = make_venue(extra={"imap": {"sender_filter": [], "subject_keywords": ["dune"]}})
    ImapNewsletterAdapter(venue, make_app_config()).fetch()

    assert len(fake.searches) == 1
    assert "FROM" not in fake.searches[0]


def test_duplicate_uids_across_sender_searches_are_fetched_once(mocker, monkeypatch, load_fixture_email):
    """Both configured senders match the same message, so the union must not double it."""
    setup_imap_env(monkeypatch)
    fake = FakeImap({b"1": load_fixture_email("science_museum_onsale_2026-09-09.eml")})
    mocker.patch("imaplib.IMAP4_SSL", return_value=fake)

    listings = ImapNewsletterAdapter(make_science_museum_venue(), make_app_config()).fetch()
    # Two showtimes from one email, not four from a doubled UID.
    assert len(listings) == 2


def test_own_order_confirmation_is_not_an_on_sale(mocker, monkeypatch, load_fixture_email):
    """Regression for the 28 Sept 2026 false alarm.

    An order confirmation from confirmation@sciencemuseum.ac.uk matched the sender
    filter, the subject keyword "tickets" and the film name, so the watcher reported the
    user's own purchase back to them as "Tickets on sale" - twice, including a bogus
    23:15 showtime scraped from the order timestamp. False alarms on the channel that
    matters are how a real alert gets ignored.
    """
    setup_imap_env(monkeypatch)
    raw = load_fixture_email("science_museum_order_confirmation.eml")
    mocker.patch("imaplib.IMAP4_SSL", return_value=FakeImap({b"1": raw}))

    listings = ImapNewsletterAdapter(make_science_museum_venue(), make_app_config()).fetch()
    assert listings == []


def test_real_announcement_still_passes_with_the_exclusions_active(mocker, monkeypatch, load_fixture_email):
    """The exclusions must not be so broad that they swallow the thing we are waiting
    for. The genuine announcement comes from no-reply@, not confirmation@, and carries
    no receipt markers."""
    setup_imap_env(monkeypatch)
    raw = load_fixture_email("science_museum_onsale_2026-09-09.eml")
    mocker.patch("imaplib.IMAP4_SSL", return_value=FakeImap({b"1": raw}))

    listings = ImapNewsletterAdapter(make_science_museum_venue(), make_app_config()).fetch()
    assert len(listings) == 2
    assert all(l.availability == "bookable" for l in listings)


@pytest.mark.parametrize("subject,body,expected_skip", [
    ("Your tickets for the Science Museum in London", "Order Number: 123 Dune IMAX 70mm", True),
    ("Password Reset Request", "Dune IMAX reset your password", True),
    ("Your Account Was Updated", "Your Account Information Dune IMAX", True),
    # An announcement that happens to be phrased like a receipt subject must survive,
    # because it carries no receipt markers in the body.
    ("Your tickets for Dune: Part Three are now on sale", "Book tickets for Dune IMAX 70mm", False),
])
def test_transactional_markers_target_receipts_not_wording(
    subject, body, expected_skip, mocker, monkeypatch
):
    setup_imap_env(monkeypatch)
    raw = (
        f"Message-ID: <t-{abs(hash(subject))}@example.invalid>\r\n"
        f"From: Science Museum <no-reply@sciencemuseum.ac.uk>\r\n"
        f"To: kazzazgili@gmail.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: Mon, 28 Sep 2026 23:19:05 +0100\r\n"
        f"Content-Type: text/plain; charset=\"utf-8\"\r\n\r\n{body}\r\n"
    ).encode()
    mocker.patch("imaplib.IMAP4_SSL", return_value=FakeImap({b"1": raw}))

    listings = ImapNewsletterAdapter(make_science_museum_venue(), make_app_config()).fetch()
    assert (listings == []) is expected_skip


def test_confirmation_sender_is_excluded_even_without_receipt_markers(mocker, monkeypatch):
    setup_imap_env(monkeypatch)
    raw = (
        b"Message-ID: <conf-1@example.invalid>\r\n"
        b"From: Science Museum <confirmation@sciencemuseum.ac.uk>\r\n"
        b"To: kazzazgili@gmail.com\r\n"
        b"Subject: Dune: Part Three IMAX 70mm tickets are on sale\r\n"
        b"Date: Mon, 28 Sep 2026 23:19:05 +0100\r\n"
        b"Content-Type: text/plain; charset=\"utf-8\"\r\n\r\nBook tickets now.\r\n"
    )
    mocker.patch("imaplib.IMAP4_SSL", return_value=FakeImap({b"1": raw}))
    assert ImapNewsletterAdapter(make_science_museum_venue(), make_app_config()).fetch() == []


def test_connection_lost_mid_scan_fails_the_poll(mocker, monkeypatch, load_fixture_email):
    """A dropped connection must not look like a successful empty poll.

    Seen in the wild: a run of "[Errno 32] Broken pipe" warnings, one per remaining
    message, followed by the cycle being recorded as a success. Every UID after the
    break went unread, so if the on-sale email was in that remainder it would never
    have been alerted on - and source_health would show the venue as healthy.
    """
    setup_imap_env(monkeypatch)

    class DyingImap(FakeImap):
        def fetch(self, uid, spec):
            raise OSError(32, "Broken pipe")

    mocker.patch(
        "imaplib.IMAP4_SSL",
        return_value=DyingImap({b"1": load_fixture_email("bfi_onsale_announcement.eml")}),
    )
    with pytest.raises(AdapterFetchError, match="connection lost"):
        ImapNewsletterAdapter(make_venue(), make_app_config()).fetch()


def test_one_unparseable_message_does_not_abandon_the_scan(mocker, monkeypatch, load_fixture_email):
    """A single malformed message is different from a dead connection: keep going."""
    setup_imap_env(monkeypatch)
    good = load_fixture_email("bfi_onsale_announcement.eml")

    class PartlyBadImap(FakeImap):
        def fetch(self, uid, spec):
            if uid == b"1":
                raise ValueError("malformed header")
            return super().fetch(uid, spec)

    mocker.patch("imaplib.IMAP4_SSL", return_value=PartlyBadImap({b"1": b"garbage", b"2": good}))
    listings = ImapNewsletterAdapter(make_venue(), make_app_config()).fetch()
    assert len(listings) == 1
