from __future__ import annotations

import argparse
import sys

from dune_watch.config import ConfigError, load_config
from dune_watch.engine.diff import build_alert, classify_transition
from dune_watch.engine.poller import run_loop, run_poll_cycle
from dune_watch.engine.state_store import StateStore
from dune_watch.models import Alert, BatchContext, RawListing
from dune_watch.notify.browser_open import BrowserOpener
from dune_watch.notify.dispatcher import Dispatcher, build_channels
from dune_watch.util.logging_setup import setup_logging

DEFAULT_CONFIG_PATH = "config/config.yaml"


def _build_dispatcher(app_config, dry_run: bool) -> Dispatcher:
    channels = build_channels(app_config)
    browser_opener = BrowserOpener(
        enabled=app_config.notifications.auto_open_enabled,
        min_urgency=app_config.notifications.auto_open_min_urgency,
    )
    return Dispatcher(channels=channels, dry_run=dry_run, browser_opener=browser_opener)


def _synthetic_listings(app_config) -> list[RawListing]:
    """Fixture data used by `run --dry-run` to exercise the full diff+notify pipeline
    without making any network or IMAP call."""
    return [
        RawListing(
            venue_id="science_museum_imax",
            venue_name="Science Museum IMAX (The Ronson Theatre)",
            film_title=app_config.film.title,
            show_date="2026-12-19", show_time="19:30",
            format_label="IMAX 70mm", availability="bookable",
            booking_link="https://www.sciencemuseum.org.uk/see-and-do/dune-part-three",
            source_type="html_page_diff", raw_fingerprint="synthetic-dry-run",
        ),
    ]


def _run_dry_run(app_config) -> int:
    store = StateStore(":memory:")
    dispatcher = _build_dispatcher(app_config, dry_run=True)
    try:
        for listing in _synthetic_listings(app_config):
            old = store.get_listing(listing.listing_key())
            result = classify_transition(old, listing, BatchContext(is_batch=False), app_config.film.format_keywords)
            store.upsert_listing(listing)
            if result:
                event_type, urgency = result
                alert = build_alert(listing, event_type, urgency)
                dispatcher.dispatch(alert)
                print(f"[DRY RUN] {alert.title}\n{alert.body}\n")
        print("Dry run complete: no network or IMAP calls were made.")
    finally:
        store.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    logger = setup_logging()
    try:
        app_config = load_config(args.config)
    except ConfigError as exc:
        logger.error("Config error: %s", exc)
        return 1

    if args.dry_run:
        return _run_dry_run(app_config)

    store = StateStore(app_config.state_db_path)
    dispatcher = _build_dispatcher(app_config, dry_run=False)
    only_venues = args.venue if args.venue else None
    try:
        if args.loop:
            run_loop(app_config, store, dispatcher)
        else:
            alerts = run_poll_cycle(app_config, store, dispatcher, only_venue_ids=only_venues)
            for alert in alerts:
                print(f"{alert.title}\n{alert.body}\n")
            if not alerts:
                print("No new alerts this cycle.")
    finally:
        store.close()
    return 0


def cmd_test_notify(args: argparse.Namespace) -> int:
    logger = setup_logging()
    try:
        app_config = load_config(args.config)
    except ConfigError as exc:
        logger.error("Config error: %s", exc)
        return 1

    channels = build_channels(app_config)
    if args.channel != "all":
        name_map = {"macos": "macos_native", "ntfy": "ntfy", "email": "email_smtp"}
        target = name_map[args.channel]
        channels = [c for c in channels if c.name == target]
        if not channels:
            print(f"Channel '{args.channel}' is not enabled in config.")
            return 1

    dispatcher = Dispatcher(channels=channels, dry_run=False, browser_opener=None)
    alert = Alert(
        listing_key="test|synthetic", venue_id="test", venue_name="Test Venue",
        film_title=app_config.film.title, show_date="2026-12-19", show_time="19:30",
        format_label="IMAX 70mm", booking_link="https://example.org/book",
        event_type="new_listing", urgency=args.urgency,
    )
    result = dispatcher.dispatch(alert)
    for name, status in result.channel_results.items():
        print(f"{name}: {status}")
    return 0


def cmd_init_db(args: argparse.Namespace) -> int:
    logger = setup_logging()
    try:
        app_config = load_config(args.config)
    except ConfigError as exc:
        logger.error("Config error: %s", exc)
        return 1
    store = StateStore(app_config.state_db_path)
    store.close()
    print(f"Initialized state DB at {app_config.state_db_path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    logger = setup_logging()
    try:
        app_config = load_config(args.config)
    except ConfigError as exc:
        logger.error("Config error: %s", exc)
        return 1
    store = StateStore(app_config.state_db_path)
    try:
        for venue in app_config.venues:
            health = store.get_source_health(venue.id)
            status_str = "never polled"
            if health:
                status_str = (
                    f"{health['consecutive_failures']} consecutive failures, "
                    f"last success {health['last_success_at']}"
                )
            print(f"{venue.id} ({'enabled' if venue.enabled else 'disabled'}): {status_str}")

        print("\nCurrent listings:")
        listings = store.all_listings()
        if not listings:
            print("  (none yet)")
        for listing in listings:
            print(
                f"  {listing.venue_name} | {listing.show_date or '?'} {listing.show_time or ''} | "
                f"{listing.format_label or '?'} | {listing.availability}"
            )
    finally:
        store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dune_watch")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Poll all enabled venues once (or continuously with --loop)")
    run_p.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    run_p.add_argument("--dry-run", action="store_true", help="Use synthetic data, no network/IMAP calls")
    run_p.add_argument("--once", action="store_true", help="Run a single poll cycle then exit (default)")
    run_p.add_argument("--loop", action="store_true", help="Run continuously in-process with per-venue scheduling")
    run_p.add_argument("--venue", action="append", help="Restrict to one venue id (repeatable)")
    run_p.set_defaults(func=cmd_run)

    test_p = sub.add_parser("test-notify", help="Send a synthetic alert through real notification channels")
    test_p.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    test_p.add_argument("--channel", choices=["macos", "ntfy", "email", "all"], default="all")
    test_p.add_argument("--urgency", choices=["INFO", "HIGH", "CRITICAL"], default="HIGH")
    test_p.set_defaults(func=cmd_test_notify)

    init_p = sub.add_parser("init-db", help="Create the state database if it doesn't exist")
    init_p.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    init_p.set_defaults(func=cmd_init_db)

    status_p = sub.add_parser("status", help="Show last poll results and current listings")
    status_p.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    status_p.set_defaults(func=cmd_status)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
