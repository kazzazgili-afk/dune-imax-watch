"""Orchestrates one poll cycle: run each enabled venue's adapter, diff results against
stored state, dispatch alerts, and keep source_health up to date. One venue's failure
never blocks the others."""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from dune_watch.adapters.base import AdapterFetchError
from dune_watch.adapters.registry import build_adapter
from dune_watch.config import AppConfig, VenueConfig
from dune_watch.engine.daily_status import maybe_send_daily_status
from dune_watch.engine.diff import build_alert, classify_transition
from dune_watch.engine.state_store import StateStore
from dune_watch.models import Alert, BatchContext
from dune_watch.notify.dispatcher import Dispatcher
from dune_watch.util.robots import get_cache

logger = logging.getLogger("dune_watch.poller")

# Events that mean "something is happening at this venue right now" - seeing one of
# these switches the venue to hot polling for a few hours.
ESCALATING_EVENTS = ("on_sale_detected", "queue_active", "sale_announced", "batch_release")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Could not parse datetime %r; ignoring", value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def venue_is_hot(
    venue: VenueConfig, app_config: AppConfig, store: StateStore, force_hot: bool = False
) -> tuple[bool, str]:
    """Whether this venue should poll at its hot cadence, and why (for logging)."""
    if force_hot:
        return True, "forced with --hot"

    now = datetime.now(timezone.utc)

    onsale = _parse_iso(venue.expected_onsale)
    if onsale is not None:
        window_opens = onsale - timedelta(hours=app_config.polling.hot_hours_before_onsale)
        if window_opens <= now <= onsale + timedelta(hours=app_config.polling.hot_hours_after_signal):
            return True, f"expected on-sale at {venue.expected_onsale}"

    last = _parse_iso(store.last_escalation_at(venue.id, ESCALATING_EVENTS))
    if last is not None and now - last <= timedelta(hours=app_config.polling.hot_hours_after_signal):
        return True, f"escalating alert at {last.isoformat()}"

    return False, "routine"


def venue_interval_seconds(
    venue: VenueConfig, app_config: AppConfig, store: StateStore, force_hot: bool = False
) -> float:
    """Effective seconds between polls: the hot or cold cadence, never faster than the
    site's published robots.txt Crawl-delay."""
    hot, _ = venue_is_hot(venue, app_config, store, force_hot)
    if hot:
        configured = float(venue.hot_interval_seconds or app_config.polling.hot_interval_seconds)
    else:
        configured = float(venue.poll_interval_minutes * 60)

    url = venue.extra.get("url")
    if not url:
        return configured
    return get_cache(app_config.polling.user_agent).effective_interval_seconds(url, configured)


def poll_venue(venue: VenueConfig, app_config: AppConfig, store: StateStore, dispatcher: Dispatcher) -> list[Alert]:
    adapter = build_adapter(venue, app_config)
    poll_id = store.record_poll_start(venue.id)
    alerts: list[Alert] = []

    try:
        listings = adapter.fetch()
    except AdapterFetchError as exc:
        logger.warning("Adapter for %s failed: %s", venue.id, exc)
        store.record_poll_finish(poll_id, success=False, error_message=str(exc), listings_found=0)
        failures = store.record_source_failure(venue.id)
        _maybe_alert_source_failing(venue, app_config, store, dispatcher, failures, str(exc))
        return alerts

    store.record_source_success(venue.id)

    new_count = sum(1 for listing in listings if store.get_listing(listing.listing_key()) is None)
    batch_context = BatchContext(is_batch=new_count >= app_config.polling.batch_threshold)

    for listing in listings:
        old = store.get_listing(listing.listing_key())
        result = classify_transition(old, listing, batch_context, app_config.film.format_keywords)
        store.upsert_listing(listing)
        if result is not None:
            event_type, urgency = result
            alert = build_alert(listing, event_type, urgency)
            dispatch_result = dispatcher.dispatch(alert)
            store.record_alert_sent(
                alert.listing_key, event_type, urgency,
                [c for c, r in dispatch_result.channel_results.items() if r == "success"],
                [c for c, r in dispatch_result.channel_results.items() if r != "success"],
            )
            alerts.append(alert)

    store.record_poll_finish(poll_id, success=True, error_message=None, listings_found=len(listings))
    return alerts


def _maybe_alert_source_failing(
    venue: VenueConfig, app_config: AppConfig, store: StateStore,
    dispatcher: Dispatcher, consecutive_failures: int, error_message: str,
) -> None:
    health = store.get_source_health(venue.id)
    already_sent = bool(health["failure_alert_sent"]) if health else False
    if consecutive_failures >= app_config.polling.failing_source_alert_after_cycles and not already_sent:
        alert = Alert(
            listing_key=f"{venue.id}|source_failure",
            venue_id=venue.id,
            venue_name=venue.name,
            film_title=app_config.film.title,
            show_date=None,
            show_time=None,
            format_label=None,
            booking_link=None,
            event_type="source_failing",
            urgency="INFO",
            detail=f"{venue.name} has failed {consecutive_failures} consecutive polls. Last error: {error_message}",
        )
        dispatcher.dispatch(alert)
        store.mark_failure_alert_sent(venue.id)
        logger.warning("Source %s has failed %d consecutive polls: %s", venue.id, consecutive_failures, error_message)


def run_poll_cycle(
    app_config: AppConfig, store: StateStore, dispatcher: Dispatcher,
    only_venue_ids: Optional[list[str]] = None,
) -> list[Alert]:
    all_alerts: list[Alert] = []
    for venue in app_config.enabled_venues():
        if only_venue_ids and venue.id not in only_venue_ids:
            continue
        all_alerts.extend(poll_venue(venue, app_config, store, dispatcher))

    # After polling, so the status reflects this cycle's results. Skipped when the run
    # was restricted to specific venues, since that is a manual spot-check.
    if not only_venue_ids:
        status = maybe_send_daily_status(app_config, store, dispatcher)
        if status is not None:
            all_alerts.append(status)
    return all_alerts


def run_loop(
    app_config: AppConfig, store: StateStore, dispatcher: Dispatcher, force_hot: bool = False
) -> None:
    """Continuous in-process loop with per-venue scheduling.

    Ticks every second rather than every 15, because hot polling runs at a 20-30s
    cadence and a 15s tick would quantise that badly. The interval is recomputed each
    time a venue is polled, so a venue flips to its hot cadence within one cycle of an
    on-sale signal without restarting the process.
    """
    venues = app_config.enabled_venues()
    next_due = {v.id: 0.0 for v in venues}
    last_hot_state: dict[str, bool] = {}
    logger.info("Starting continuous loop over %d venue(s) (Ctrl+C to stop)", len(venues))
    try:
        while True:
            now = time.monotonic()
            for venue in venues:
                if now < next_due[venue.id]:
                    continue

                hot, reason = venue_is_hot(venue, app_config, store, force_hot)
                if last_hot_state.get(venue.id) != hot:
                    logger.info("%s polling cadence: %s (%s)", venue.id,
                                "HOT" if hot else "routine", reason)
                    last_hot_state[venue.id] = hot

                poll_venue(venue, app_config, store, dispatcher)

                interval = venue_interval_seconds(venue, app_config, store, force_hot)
                # Jitter stays proportionate: a 90s jitter on a 20s hot interval would
                # undo the whole point of polling fast.
                jitter_cap = min(app_config.polling.jitter_seconds, interval * 0.25)
                next_due[venue.id] = time.monotonic() + interval + random.uniform(0, jitter_cap)

            _maybe_alert_heartbeat_stale(app_config, store, dispatcher)
            maybe_send_daily_status(app_config, store, dispatcher)
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Loop stopped by user")


_last_heartbeat_alert: dict[str, float] = {}


def _maybe_alert_heartbeat_stale(
    app_config: AppConfig, store: StateStore, dispatcher: Dispatcher
) -> None:
    """Notice silence. The failure mode this catches is the host sleeping or losing DNS,
    where every venue fails and every notification channel fails too, so the absence of
    alerts looks exactly like 'nothing has happened yet'."""
    stale_after = app_config.polling.heartbeat_stale_after_minutes
    if not stale_after:
        return

    now = datetime.now(timezone.utc)
    freshest: Optional[datetime] = None
    for venue in app_config.enabled_venues():
        health = store.get_source_health(venue.id)
        last = _parse_iso(health["last_success_at"]) if health else None
        if last is not None and (freshest is None or last > freshest):
            freshest = last

    if freshest is not None and now - freshest <= timedelta(minutes=stale_after):
        return

    # Re-alert at most once per stale window, so a long outage sends a handful of
    # notices rather than one per second.
    key = "heartbeat"
    last_sent = _last_heartbeat_alert.get(key)
    if last_sent is not None and time.monotonic() - last_sent < stale_after * 60:
        return
    _last_heartbeat_alert[key] = time.monotonic()

    detail = (
        f"No venue has polled successfully in over {stale_after} minutes "
        f"(last success: {freshest.isoformat() if freshest else 'never'}). "
        "The watcher may be offline - check network and logs."
    )
    dispatcher.dispatch(Alert(
        listing_key="watcher|heartbeat", venue_id="watcher", venue_name="Watcher",
        film_title=app_config.film.title, show_date=None, show_time=None,
        format_label=None, booking_link=None, event_type="source_failing",
        urgency="HIGH", detail=detail,
    ))
    logger.error("Heartbeat stale: %s", detail)
