"""One "still watching" notification per calendar day.

The watcher's whole value is that it is running when you are not looking at it, which
means silence is ambiguous: a healthy watcher with nothing to report and a dead watcher
produce exactly the same experience. This closes that gap with a positive daily signal -
if the ping stops arriving, something is broken.

It fires on the first poll cycle of each calendar day at or after `not_before_hour`, and
records the date in the state DB. That design matters because GitHub Actions throttles
scheduled workflows hard (observed: ~6 runs a day with gaps up to 8 hours), so anything
that depends on a run happening at a *particular* time would silently never fire. "First
run of the day that qualifies" works regardless of when the runs actually land.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from dune_watch.config import AppConfig
from dune_watch.engine.state_store import StateStore
from dune_watch.models import Alert

logger = logging.getLogger("dune_watch.daily_status")

# Kept in step with engine/poller.ESCALATING_EVENTS, imported lazily to avoid a cycle.
ESCALATING_EVENTS = ("on_sale_detected", "queue_active", "sale_announced", "batch_release")

META_KEY = "last_daily_status_date"


def _now(tz_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        logger.warning("Unknown daily_status timezone %r; falling back to UTC", tz_name)
        return datetime.now(ZoneInfo("UTC"))


def _short(url: str, limit: int = 60) -> str:
    return url if len(url) <= limit else url[:limit] + "..."


def build_status_body(app_config: AppConfig, store: StateStore, tz_name: str) -> str:
    """A two-second read: is it healthy, and has anything actually happened?

    Deliberately does NOT list every listing ever marked bookable. Listings derived from
    a newsletter or a social post are *announcements*, not live availability - a mail
    from three weeks ago about a screening that has since sold out is not something to
    repeat every morning. What matters daily is: are the sources healthy, and was there
    a new on-sale signal since yesterday.
    """
    now = _now(tz_name)
    day_ago = (now - timedelta(hours=24)).astimezone(ZoneInfo("UTC")).isoformat()

    healthy, degraded = 0, []
    for venue in app_config.enabled_venues():
        health = store.get_source_health(venue.id)
        failures = health["consecutive_failures"] if health else 0
        last_ok = (health["last_success_at"] or "")[:16] if health else ""
        if health and last_ok and not failures:
            healthy += 1
        else:
            degraded.append(f"{venue.id} ({failures} fails, last ok {last_ok or 'never'})")

    total = len(app_config.enabled_venues())
    lines = [f"{app_config.film.title} - IMAX 70mm watch is running."]
    lines.append(f"Sources healthy: {healthy}/{total}" + (" - ALL OK" if healthy == total else ""))
    if degraded:
        lines.append("Needs attention: " + "; ".join(degraded))

    recent = store.alerts_since(day_ago, ESCALATING_EVENTS)
    if recent:
        lines.append("")
        lines.append(f"*** {len(recent)} new on-sale signal(s) in the last 24h ***")
        for row in recent[:4]:
            lines.append(f"  {row['urgency']} {row['event_type']} at {row['sent_at'][:16]}")
        live = [l for l in store.all_listings()
                if l.availability == "bookable" and (l.last_alerted_at or "") >= day_ago]
        for listing in live[:4]:
            when = " ".join(p for p in [listing.show_date, listing.show_time] if p) or "date TBC"
            lines.append(f"  -> {listing.venue_name}: {when}")
            if listing.booking_link:
                lines.append(f"     {_short(listing.booking_link, 70)}")
    else:
        prior = store.alerts_since("1970-01-01", ESCALATING_EVENTS)
        if prior:
            lines.append(f"No new on-sale since {prior[0]['sent_at'][:16]}.")
        else:
            lines.append("No on-sale detected yet.")

    lines.append("")
    lines.append(f"({now.strftime('%a %d %b, %H:%M %Z')})")
    return "\n".join(lines)


def maybe_send_daily_status(
    app_config: AppConfig, store: StateStore, dispatcher, force: bool = False
) -> Optional[Alert]:
    """Send today's status if it hasn't been sent yet. Returns the Alert, or None."""
    notif = app_config.notifications
    if not force and not notif.daily_status_enabled:
        return None

    now = _now(notif.daily_status_timezone)
    today = now.date().isoformat()

    if not force:
        if now.hour < notif.daily_status_not_before_hour:
            return None
        if store.get_meta(META_KEY) == today:
            return None

    alert = Alert(
        listing_key="watcher|daily_status",
        venue_id="watcher",
        venue_name="dune-imax-watch",
        film_title=app_config.film.title,
        show_date=None,
        show_time=None,
        format_label=None,
        booking_link=None,
        event_type="daily_status",
        # INFO on purpose: it must never trigger the urgent ntfy priority, the repeat
        # nudges or the browser auto-open reserved for an actual on-sale.
        urgency="INFO",
        detail=build_status_body(app_config, store, notif.daily_status_timezone),
    )
    result = dispatcher.dispatch(alert)

    # Only record the day as done if the notification actually left the building.
    # Marking it sent after a total delivery failure would mean no ping arrives at all
    # that day while the watcher believes it reported in - precisely the ambiguity this
    # feature exists to remove. Leaving the date unset makes the next poll cycle try
    # again, which is a better retry than hammering inside one cycle.
    delivered = True
    channel_results = getattr(result, "channel_results", None)
    if channel_results:
        delivered = any(status == "success" for status in channel_results.values())

    if not delivered:
        logger.warning(
            "Daily status could not be delivered on any channel (%s); will retry next cycle",
            ", ".join(f"{k}: {v}" for k, v in (channel_results or {}).items()),
        )
        return alert

    store.set_meta(META_KEY, today)
    logger.info("Sent daily status for %s", today)
    return alert
