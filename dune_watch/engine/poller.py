"""Orchestrates one poll cycle: run each enabled venue's adapter, diff results against
stored state, dispatch alerts, and keep source_health up to date. One venue's failure
never blocks the others."""
from __future__ import annotations

import logging
import random
import time
from typing import Optional

from dune_watch.adapters.base import AdapterFetchError
from dune_watch.adapters.registry import build_adapter
from dune_watch.config import AppConfig, VenueConfig
from dune_watch.engine.diff import build_alert, classify_transition
from dune_watch.engine.state_store import StateStore
from dune_watch.models import Alert, BatchContext
from dune_watch.notify.dispatcher import Dispatcher

logger = logging.getLogger("dune_watch.poller")


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
    return all_alerts


def run_loop(app_config: AppConfig, store: StateStore, dispatcher: Dispatcher) -> None:
    """Continuous in-process loop with per-venue interval scheduling, for ad-hoc local
    runs without launchd/systemd installed yet."""
    venues = app_config.enabled_venues()
    next_due = {v.id: 0.0 for v in venues}
    logger.info("Starting continuous loop over %d venue(s) (Ctrl+C to stop)", len(venues))
    try:
        while True:
            now = time.monotonic()
            for venue in venues:
                if now >= next_due[venue.id]:
                    poll_venue(venue, app_config, store, dispatcher)
                    jitter = random.uniform(0, app_config.polling.jitter_seconds)
                    next_due[venue.id] = time.monotonic() + venue.poll_interval_minutes * 60 + jitter
            time.sleep(15)
    except KeyboardInterrupt:
        logger.info("Loop stopped by user")
