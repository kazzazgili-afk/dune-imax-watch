"""Normalized data shapes shared by every adapter, the diff engine, and notifications."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

AVAILABILITY_STATES = ("unknown", "register_interest", "unavailable", "bookable", "sold_out")
URGENCY_LEVELS = ("INFO", "HIGH", "CRITICAL")

_EVENT_LABELS = {
    "new_listing": "New listing",
    "batch_release": "Batch release",
    "availability_change": "Availability changed",
    "sale_announced": "Tickets on sale",
    "info_change": "Listing updated",
    "source_failing": "Source check failing",
}


@dataclass
class RawListing:
    """One normalized listing as returned by an adapter's fetch() for a single poll cycle."""

    venue_id: str
    venue_name: str
    film_title: str
    show_date: Optional[str]
    show_time: Optional[str]
    format_label: Optional[str]
    availability: str
    booking_link: Optional[str]
    source_type: str
    raw_fingerprint: str
    source_message_id: Optional[str] = None

    def listing_key(self) -> str:
        if self.source_type == "imap_newsletter":
            return f"{self.venue_id}|email|{self.source_message_id}"
        if self.show_date is None and self.show_time is None:
            return f"{self.venue_id}|pending"
        return f"{self.venue_id}|{self.show_date}|{self.show_time}|{self.format_label}"


@dataclass
class StoredListing:
    """The persisted row shape read back from state_store — field names match SQL columns."""

    listing_key: str
    venue_id: str
    venue_name: str
    film_title: str
    show_date: Optional[str]
    show_time: Optional[str]
    format_label: Optional[str]
    availability: str
    booking_link: Optional[str]
    source_type: str
    raw_fingerprint: str
    first_seen_at: str
    last_seen_at: str
    last_alerted_at: Optional[str] = None
    last_alert_urgency: Optional[str] = None


@dataclass
class BatchContext:
    """Whether this venue's poll cycle produced a 'batch' of brand-new listings at once."""

    is_batch: bool


@dataclass
class Alert:
    listing_key: str
    venue_id: str
    venue_name: str
    film_title: str
    show_date: Optional[str]
    show_time: Optional[str]
    format_label: Optional[str]
    booking_link: Optional[str]
    event_type: str
    urgency: str
    detail: Optional[str] = None

    @property
    def title(self) -> str:
        label = _EVENT_LABELS.get(self.event_type, "Update")
        fmt = f" ({self.format_label})" if self.format_label else ""
        return f"[{self.urgency}] {self.film_title} - {self.venue_name}{fmt}: {label}"

    @property
    def body(self) -> str:
        if self.event_type == "source_failing":
            return self.detail or "Repeated polling failures - check the logs."
        when = " ".join(part for part in [self.show_date, self.show_time] if part) or "Date/time TBC"
        link = self.booking_link or "No booking link found - check the venue site"
        return f"{when}\n{link}"
