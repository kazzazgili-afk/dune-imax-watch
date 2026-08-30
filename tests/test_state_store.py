from __future__ import annotations

from dune_watch.engine.state_store import StateStore
from dune_watch.models import RawListing


def make_listing(**overrides) -> RawListing:
    defaults = dict(
        venue_id="science_museum_imax", venue_name="Science Museum IMAX",
        film_title="Dune: Part Three", show_date="2026-12-19", show_time="19:30",
        format_label="IMAX 70mm", availability="bookable", booking_link="https://example.org/book",
        source_type="html_page_diff", raw_fingerprint="fp1",
    )
    defaults.update(overrides)
    return RawListing(**defaults)


def test_schema_creation_is_idempotent(tmp_path):
    db_path = tmp_path / "state.db"
    store1 = StateStore(db_path)
    store1.close()
    store2 = StateStore(db_path)  # should not raise on an already-initialized DB
    store2.close()


def test_upsert_inserts_new_and_updates_existing(fresh_state_db):
    store = fresh_state_db
    listing = make_listing()
    store.upsert_listing(listing)
    stored = store.get_listing(listing.listing_key())
    assert stored is not None
    assert stored.availability == "bookable"
    first_seen = stored.first_seen_at

    updated = make_listing(availability="sold_out")
    store.upsert_listing(updated)
    stored_again = store.get_listing(listing.listing_key())
    assert stored_again.availability == "sold_out"
    assert stored_again.first_seen_at == first_seen  # first_seen_at preserved across updates


def test_get_listing_returns_none_when_absent(fresh_state_db):
    assert fresh_state_db.get_listing("does-not-exist") is None


def test_source_health_increment_and_reset(fresh_state_db):
    store = fresh_state_db
    assert store.record_source_failure("science_museum_imax") == 1
    assert store.record_source_failure("science_museum_imax") == 2
    store.record_source_success("science_museum_imax")
    health = store.get_source_health("science_museum_imax")
    assert health["consecutive_failures"] == 0


def test_failure_alert_sent_flag(fresh_state_db):
    store = fresh_state_db
    store.record_source_failure("bfi_imax")
    store.mark_failure_alert_sent("bfi_imax")
    health = store.get_source_health("bfi_imax")
    assert health["failure_alert_sent"] == 1
    store.record_source_success("bfi_imax")
    health = store.get_source_health("bfi_imax")
    assert health["failure_alert_sent"] == 0


def test_alerts_sent_recorded(fresh_state_db):
    store = fresh_state_db
    listing = make_listing()
    store.upsert_listing(listing)
    store.record_alert_sent(listing.listing_key(), "new_listing", "HIGH", ["ntfy"], ["email_smtp"])
    stored = store.get_listing(listing.listing_key())
    assert stored.last_alert_urgency == "HIGH"
    assert stored.last_alerted_at is not None


def test_all_listings_returns_everything(fresh_state_db):
    store = fresh_state_db
    store.upsert_listing(make_listing(show_time="19:30"))
    store.upsert_listing(make_listing(show_time="21:00"))
    listings = store.all_listings()
    assert len(listings) == 2
