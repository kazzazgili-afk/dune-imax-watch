"""Pure classification logic: given the previously stored state of a listing (or None
if never seen) and the freshly fetched listing, decide whether an alert should fire
and at what urgency. No I/O here - fully unit-testable with plain objects."""
from __future__ import annotations

from typing import Optional

from dune_watch.models import Alert, BatchContext, RawListing, StoredListing

_NOT_YET_BOOKABLE = {"unknown", "unavailable", "sold_out", "register_interest", "queue_active"}

# Sources whose items are immutable once published: an email or a post never changes,
# so each one is only ever newsworthy the first time it is seen.
_MESSAGE_SOURCES = {"imap_newsletter", "bluesky_feed"}


def _is_imax_or_70mm(listing: RawListing, format_keywords: list[str]) -> bool:
    label = (listing.format_label or "").lower()
    if not label:
        return False
    return any(keyword.lower() in label for keyword in format_keywords)


def classify_transition(
    old: Optional[StoredListing],
    new: RawListing,
    batch_context: BatchContext,
    format_keywords: list[str],
) -> Optional[tuple[str, str]]:
    """Returns (event_type, urgency) or None if no alert is warranted.

    `batch_context.is_batch` is computed once per venue per poll cycle by the caller
    (engine/poller.py) - it's a property of how many new listings appeared together
    in this cycle, not of any single listing.
    """
    is_imax = _is_imax_or_70mm(new, format_keywords)

    if new.source_type in _MESSAGE_SOURCES:
        # Immutable once published, so only newsworthy the first time we see it. Without
        # this guard every matching item in the lookback window re-alerts on every poll.
        if old is not None:
            return None
        if new.availability == "bookable":
            return ("on_sale_detected", "CRITICAL" if is_imax else "HIGH")
        return ("sale_announced", "HIGH")

    if old is None:
        if new.availability == "queue_active":
            return ("queue_active", "CRITICAL")
        if is_imax and batch_context.is_batch:
            return ("batch_release", "CRITICAL")
        if is_imax:
            return ("new_listing", "HIGH")
        return ("new_listing", "INFO")

    if old.availability in _NOT_YET_BOOKABLE and new.availability == "bookable":
        return ("on_sale_detected", "CRITICAL" if is_imax else "HIGH")

    # A waiting room appearing in front of booking means a sale is imminent or running.
    if new.availability == "queue_active" and old.availability != "queue_active":
        return ("queue_active", "CRITICAL")

    if old.availability == "register_interest" and new.availability != "register_interest":
        return ("sale_announced", "HIGH")

    if old.availability == "bookable" and new.availability == "sold_out":
        return ("availability_change", "INFO")

    if old.raw_fingerprint == new.raw_fingerprint and old.availability == new.availability:
        return None

    return ("info_change", "INFO")


def build_alert(new: RawListing, event_type: str, urgency: str) -> Alert:
    return Alert(
        listing_key=new.listing_key(),
        venue_id=new.venue_id,
        venue_name=new.venue_name,
        film_title=new.film_title,
        show_date=new.show_date,
        show_time=new.show_time,
        format_label=new.format_label,
        booking_link=new.booking_link,
        event_type=event_type,
        urgency=urgency,
    )
