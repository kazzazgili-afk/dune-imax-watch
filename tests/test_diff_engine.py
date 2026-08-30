from __future__ import annotations

from dune_watch.engine.diff import build_alert, classify_transition
from dune_watch.models import BatchContext, RawListing, StoredListing

FORMAT_KEYWORDS = ["imax 70mm", "70mm", "imax"]


def make_raw(**overrides) -> RawListing:
    defaults = dict(
        venue_id="science_museum_imax", venue_name="Science Museum IMAX",
        film_title="Dune: Part Three", show_date="2026-12-19", show_time="19:30",
        format_label="IMAX 70mm", availability="bookable", booking_link="https://example.org/book",
        source_type="html_page_diff", raw_fingerprint="fp1",
    )
    defaults.update(overrides)
    return RawListing(**defaults)


def make_stored(**overrides) -> StoredListing:
    defaults = dict(
        listing_key="science_museum_imax|2026-12-19|19:30|IMAX 70mm",
        venue_id="science_museum_imax", venue_name="Science Museum IMAX",
        film_title="Dune: Part Three", show_date="2026-12-19", show_time="19:30",
        format_label="IMAX 70mm", availability="unavailable", booking_link=None,
        source_type="html_page_diff", raw_fingerprint="fp0",
        first_seen_at="t0", last_seen_at="t0",
    )
    defaults.update(overrides)
    return StoredListing(**defaults)


def classify(old, new, is_batch=False):
    return classify_transition(old, new, BatchContext(is_batch=is_batch), FORMAT_KEYWORDS)


def test_new_listing_imax_not_batch_is_high():
    assert classify(None, make_raw(), is_batch=False) == ("new_listing", "HIGH")


def test_new_listing_imax_batch_is_critical():
    assert classify(None, make_raw(), is_batch=True) == ("batch_release", "CRITICAL")


def test_new_listing_non_imax_is_info():
    assert classify(None, make_raw(format_label="Digital"), is_batch=False) == ("new_listing", "INFO")


def test_unavailable_to_bookable_imax_is_critical():
    old = make_stored(availability="unavailable")
    new = make_raw(availability="bookable")
    assert classify(old, new) == ("availability_change", "CRITICAL")


def test_unavailable_to_bookable_non_imax_is_high():
    old = make_stored(availability="unavailable", format_label="Digital")
    new = make_raw(availability="bookable", format_label="Digital")
    assert classify(old, new) == ("availability_change", "HIGH")


def test_register_interest_to_unavailable_is_sale_announced():
    old = make_stored(availability="register_interest")
    new = make_raw(availability="unavailable")
    assert classify(old, new) == ("sale_announced", "HIGH")


def test_register_interest_to_bookable_prefers_availability_change():
    # Both rows could apply; the more specific "went bookable" rule wins.
    old = make_stored(availability="register_interest")
    new = make_raw(availability="bookable")
    assert classify(old, new) == ("availability_change", "CRITICAL")


def test_bookable_to_sold_out_is_info():
    old = make_stored(availability="bookable")
    new = make_raw(availability="sold_out")
    assert classify(old, new) == ("availability_change", "INFO")


def test_unchanged_fingerprint_and_availability_is_none():
    old = make_stored(availability="bookable", raw_fingerprint="same")
    new = make_raw(availability="bookable", raw_fingerprint="same")
    assert classify(old, new) is None


def test_other_change_is_info_change():
    old = make_stored(availability="bookable", raw_fingerprint="fp-old", show_time="19:30")
    new = make_raw(availability="bookable", raw_fingerprint="fp-new", show_time="20:00")
    assert classify(old, new) == ("info_change", "INFO")


def test_imap_listing_always_sale_announced():
    new = make_raw(source_type="imap_newsletter", source_message_id="msg-1", availability="unknown")
    assert classify(None, new) == ("sale_announced", "HIGH")


def test_build_alert_copies_fields():
    listing = make_raw()
    alert = build_alert(listing, "new_listing", "HIGH")
    assert alert.venue_id == listing.venue_id
    assert alert.urgency == "HIGH"
    assert alert.event_type == "new_listing"
    assert alert.booking_link == listing.booking_link
