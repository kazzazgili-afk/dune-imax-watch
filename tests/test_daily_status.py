from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dune_watch.config import AppConfig, FilmConfig, NotificationsConfig, PollingConfig, VenueConfig
from dune_watch.engine.daily_status import META_KEY, build_status_body, maybe_send_daily_status
from dune_watch.models import RawListing


class RecordingDispatcher:
    def __init__(self):
        self.sent = []

    def dispatch(self, alert):
        self.sent.append(alert)
        return None


def make_config(enabled=True, tz="Europe/London", not_before=9) -> AppConfig:
    return AppConfig(
        film=FilmConfig(
            title="Dune: Part Three", keywords=["dune"],
            format_keywords=["imax 70mm", "70mm", "imax"],
            opening_window_start="2026-12-01", opening_window_end="2027-02-28",
        ),
        polling=PollingConfig(),
        venues=[
            VenueConfig(id="science_museum_imax", name="Science Museum IMAX", enabled=True,
                        venue_type="html_page_diff", poll_interval_minutes=5),
            VenueConfig(id="bfi_imax", name="BFI IMAX", enabled=True,
                        venue_type="imap_newsletter", poll_interval_minutes=2),
        ],
        state_db_path=":memory:",
        notifications=NotificationsConfig(
            channels={}, auto_open_enabled=False, auto_open_min_urgency="HIGH",
            daily_status_enabled=enabled, daily_status_timezone=tz,
            daily_status_not_before_hour=not_before,
        ),
    )


def test_sends_once_then_not_again_the_same_day(fresh_state_db):
    config, store, dispatcher = make_config(not_before=0), fresh_state_db, RecordingDispatcher()

    first = maybe_send_daily_status(config, store, dispatcher)
    assert first is not None
    assert len(dispatcher.sent) == 1

    # A second cycle minutes later must not ping again.
    assert maybe_send_daily_status(config, store, dispatcher) is None
    assert len(dispatcher.sent) == 1


def test_a_new_day_pings_again(fresh_state_db):
    config, store, dispatcher = make_config(not_before=0), fresh_state_db, RecordingDispatcher()
    maybe_send_daily_status(config, store, dispatcher)
    # Simulate yesterday's ping having been the last one.
    store.set_meta(META_KEY, "2020-01-01")
    assert maybe_send_daily_status(config, store, dispatcher) is not None
    assert len(dispatcher.sent) == 2


def test_waits_until_not_before_hour(fresh_state_db):
    """Guards against pinging at 03:00 just because that is when CI happened to run."""
    config = make_config(not_before=23, tz="UTC")
    store, dispatcher = fresh_state_db, RecordingDispatcher()
    now_hour = datetime.now(timezone.utc).hour
    if now_hour >= 23:
        pytest.skip("test is time-of-day dependent; skipped during the 23:00 hour")
    assert maybe_send_daily_status(config, store, dispatcher) is None
    assert dispatcher.sent == []


def test_disabled_sends_nothing(fresh_state_db):
    config = make_config(enabled=False, not_before=0)
    dispatcher = RecordingDispatcher()
    assert maybe_send_daily_status(config, fresh_state_db, dispatcher) is None
    assert dispatcher.sent == []


def test_force_overrides_both_the_hour_and_the_once_a_day_guard(fresh_state_db):
    config = make_config(enabled=False, not_before=23)
    store, dispatcher = fresh_state_db, RecordingDispatcher()
    assert maybe_send_daily_status(config, store, dispatcher, force=True) is not None
    assert maybe_send_daily_status(config, store, dispatcher, force=True) is not None
    assert len(dispatcher.sent) == 2


def test_status_is_info_so_it_never_screams(fresh_state_db):
    """It must not inherit the urgent ntfy priority, repeat nudges or browser auto-open
    that a real on-sale gets - otherwise the daily ping trains you to ignore alerts."""
    alert = maybe_send_daily_status(
        make_config(not_before=0), fresh_state_db, RecordingDispatcher()
    )
    assert alert.urgency == "INFO"
    assert alert.booking_link is None
    assert "Watcher is live" in alert.title


def test_body_reports_health_and_quiet(fresh_state_db):
    store = fresh_state_db
    store.record_source_success("science_museum_imax")
    store.record_source_success("bfi_imax")
    body = build_status_body(make_config(), store, "Europe/London")
    assert "Sources healthy: 2/2" in body
    assert "ALL OK" in body
    assert "No on-sale detected yet." in body


def test_body_flags_a_degraded_source(fresh_state_db):
    store = fresh_state_db
    store.record_source_success("science_museum_imax")
    store.record_source_failure("bfi_imax")
    body = build_status_body(make_config(), store, "Europe/London")
    assert "Sources healthy: 1/2" in body
    assert "Needs attention" in body and "bfi_imax" in body


def test_body_surfaces_a_fresh_on_sale_with_its_link(fresh_state_db):
    store = fresh_state_db
    store.record_source_success("science_museum_imax")
    store.record_source_success("bfi_imax")
    listing = RawListing(
        venue_id="science_museum_imax", venue_name="Science Museum IMAX",
        film_title="Dune: Part Three", show_date="2026-12-18", show_time="19:00",
        format_label="IMAX 70mm", availability="bookable",
        booking_link="https://my.sciencemuseum.org.uk/events?view=calendar&kid=812",
        source_type="html_page_diff", raw_fingerprint="fp",
    )
    store.upsert_listing(listing)
    store.record_alert_sent(listing.listing_key(), "on_sale_detected", "CRITICAL", ["ntfy"], [])

    body = build_status_body(make_config(), store, "Europe/London")
    assert "new on-sale signal" in body
    assert "2026-12-18 19:00" in body
    assert "kid=812" in body


def test_body_does_not_repeat_stale_announcements(fresh_state_db):
    """A newsletter from weeks ago about a now-sold-out screening is not news today."""
    store = fresh_state_db
    store.record_source_success("science_museum_imax")
    store.record_source_success("bfi_imax")
    old = RawListing(
        venue_id="bfi_imax", venue_name="BFI IMAX", film_title="Dune: Part Three",
        show_date="2026-12-15", show_time="14:30", format_label="IMAX 70mm",
        availability="bookable", booking_link="https://example.org/old",
        source_type="imap_newsletter", raw_fingerprint="fp", source_message_id="<old>",
    )
    store.upsert_listing(old)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    store.conn.execute(
        "INSERT INTO alerts_sent (listing_key, event_type, urgency, sent_at) VALUES (?,?,?,?)",
        (old.listing_key(), "on_sale_detected", "CRITICAL", long_ago),
    )
    store.conn.commit()

    body = build_status_body(make_config(), store, "Europe/London")
    assert "new on-sale signal" not in body
    assert "No new on-sale since" in body
    assert "example.org/old" not in body


class FailingDispatcher:
    """Mimics Dispatcher's return shape with every channel failing."""

    def __init__(self):
        self.sent = []
        self.results = {"ntfy": "failed: timeout", "email_smtp": "failed: timeout"}

    def dispatch(self, alert):
        self.sent.append(alert)
        results = self.results

        class Result:
            channel_results = results

        return Result()


def test_total_delivery_failure_is_retried_next_cycle(fresh_state_db):
    """If no channel accepted the ping, the day must NOT be marked done - otherwise no
    status arrives at all that day while the watcher thinks it reported in. This was a
    real bug: both ntfy and SMTP timed out and the day was consumed anyway."""
    config, store = make_config(not_before=0), fresh_state_db
    dispatcher = FailingDispatcher()

    assert maybe_send_daily_status(config, store, dispatcher) is not None
    assert store.get_meta(META_KEY) is None, "a failed send must not consume the day"

    assert maybe_send_daily_status(config, store, dispatcher) is not None
    assert len(dispatcher.sent) == 2


def test_partial_delivery_counts_as_delivered(fresh_state_db):
    """One channel landing is enough; re-pinging would just duplicate it there."""
    config, store = make_config(not_before=0), fresh_state_db
    dispatcher = FailingDispatcher()
    dispatcher.results = {"ntfy": "failed: timeout", "email_smtp": "success"}

    maybe_send_daily_status(config, store, dispatcher)
    assert store.get_meta(META_KEY) is not None
    assert maybe_send_daily_status(config, store, dispatcher) is None
    assert len(dispatcher.sent) == 1
