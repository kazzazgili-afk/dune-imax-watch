from __future__ import annotations

import argparse
import sys

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from dune_watch.config import ConfigError, load_config
from dune_watch.engine.diff import build_alert, classify_transition
from dune_watch.engine.poller import (
    run_loop,
    run_poll_cycle,
    venue_interval_seconds,
    venue_is_hot,
)
from dune_watch.engine.state_store import StateStore
from dune_watch.models import Alert, BatchContext, RawListing
from dune_watch.notify.browser_open import BrowserOpener
from dune_watch.notify.dispatcher import Dispatcher, build_channels
from dune_watch.util.logging_setup import setup_logging
from dune_watch.util.robots import get_cache

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
            run_loop(app_config, store, dispatcher, force_hot=args.hot)
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


def cmd_robots_check(args: argparse.Namespace) -> int:
    """Print, per venue, what robots.txt permits and the interval that results.

    This is the audit trail for polling faster than the config says: the effective
    interval is always the slower of the configured cadence and the site's own
    Crawl-delay, and a disallowed path is refused outright.
    """
    logger = setup_logging()
    try:
        app_config = load_config(args.config)
    except ConfigError as exc:
        logger.error("Config error: %s", exc)
        return 1

    store = StateStore(app_config.state_db_path)
    cache = get_cache(app_config.polling.user_agent)
    exit_code = 0
    try:
        print(f"User-Agent: {app_config.polling.user_agent}\n")
        for venue in app_config.venues:
            state = "enabled" if venue.enabled else "disabled"
            url = venue.extra.get("url")
            print(f"{venue.id} ({state}, {venue.venue_type})")
            if not url:
                print("  no URL - this venue reads a mailbox or API, robots.txt N/A\n")
                continue

            verdict = cache.check(url)
            hot, reason = venue_is_hot(venue, app_config, store, force_hot=False)
            cold_s = venue.poll_interval_minutes * 60
            hot_s = venue.hot_interval_seconds or app_config.polling.hot_interval_seconds
            eff_cold = cache.effective_interval_seconds(url, cold_s)
            eff_hot = cache.effective_interval_seconds(url, hot_s)

            print(f"  url        {url}")
            print(f"  robots     {verdict.describe()}")
            print(f"  cadence    {'HOT' if hot else 'routine'} ({reason})")
            print(f"  routine    configured {cold_s:g}s -> effective {eff_cold:g}s")
            print(f"  hot        configured {hot_s:g}s -> effective {eff_hot:g}s")
            if not verdict.allowed:
                print("  RESULT     WOULD REFUSE TO FETCH (robots.txt disallows this path)")
                if venue.enabled:
                    exit_code = 2
            elif eff_hot > hot_s:
                print(f"  RESULT     allowed; hot cadence raised to the site's crawl-delay")
            else:
                print("  RESULT     allowed")
            print()
    finally:
        store.close()
    return exit_code


def _resolve_prearm_url(app_config, venue_id: str) -> str:
    venue = app_config.venue_by_id(venue_id)
    if venue is None:
        raise ConfigError(f"No venue with id '{venue_id}' in config")
    url = venue.extra.get("booking_url") or venue.extra.get("url")
    if not url:
        raise ConfigError(
            f"Venue '{venue_id}' has no 'booking_url' or 'url' to open. Add a booking_url "
            f"pointing at the venue's official booking page."
        )
    return url


def _parse_prearm_at(raw: str, tz_name: Optional[str] = None) -> datetime:
    """Accepts 'HH:MM' (next occurrence) or a full ISO 8601 datetime.

    `tz_name` matters more than it looks: venues announce on-sale times in UK local
    time, and this machine may not be on UK time. Defaulting HH:MM to the machine's
    zone silently shifts the pre-arm by the offset, so `--tz Europe/London` exists to
    say "the time I typed is the venue's time".
    """
    if tz_name:
        try:
            tzinfo = ZoneInfo(tz_name)
        except Exception as exc:
            raise ConfigError(f"Unknown timezone {tz_name!r}") from exc
    else:
        tzinfo = datetime.now().astimezone().tzinfo

    now = datetime.now(tzinfo)
    try:
        if len(raw) <= 5 and ":" in raw:
            hour, minute = (int(part) for part in raw.split(":"))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tzinfo)
    except ValueError as exc:
        raise ConfigError(f"Could not parse --at {raw!r}; use HH:MM or an ISO datetime") from exc


def cmd_prearm(args: argparse.Namespace) -> int:
    """Open the official booking page in your own logged-in browser and hand it over.

    Because Queue-it randomises everyone who is waiting when the sale opens, arriving
    before the countdown ends is what improves your odds - not speed afterwards.
    """
    logger = setup_logging()
    from dune_watch.assist.warm_browser import (
        DEFAULT_PROFILE_DIR, WarmBrowserError, open_and_hold, wait_until,
    )

    try:
        app_config = load_config(args.config)
        url = args.url or _resolve_prearm_url(app_config, args.venue)
    except ConfigError as exc:
        logger.error("Config error: %s", exc)
        return 1

    profile_dir = args.profile_dir or DEFAULT_PROFILE_DIR
    if args.at:
        try:
            target = _parse_prearm_at(args.at, args.tz)
        except ConfigError as exc:
            logger.error("%s", exc)
            return 1
        # Print both zones: an off-by-one-timezone pre-arm is a silent miss.
        local = target.astimezone()
        london = target.astimezone(ZoneInfo("Europe/London"))
        logger.info(
            "Pre-arm at %s  (London: %s | this machine: %s)",
            target.isoformat(timespec="seconds"),
            london.strftime("%Y-%m-%d %H:%M %Z"),
            local.strftime("%Y-%m-%d %H:%M %Z"),
        )
        logger.info("Will open %s", url)
        if args.dry_run:
            print(f"[DRY RUN] Would wait until {target.isoformat(timespec='seconds')} "
                  f"(London {target.astimezone(ZoneInfo('Europe/London')).strftime('%H:%M %Z')}) "
                  f"then open {url}")
            return 0
        wait_until(target.astimezone(timezone.utc))

    if args.dry_run:
        print(f"[DRY RUN] Would open {url} in the Chromium profile at {profile_dir}")
        print("[DRY RUN] No seats would be selected and no payment would be entered.")
        return 0

    print(f"Opening {url}")
    print("This does not select seats or pay - you take over once the page is up.")
    try:
        report = open_and_hold(
            url, profile_dir=profile_dir,
            hold_seconds=args.hold_seconds if args.hold_seconds else None,
        )
    except WarmBrowserError as exc:
        logger.error("%s", exc)
        return 1

    print(report.describe())
    if report.signed_in is False:
        print("You are not signed in. Sign in now in that window - it will be remembered "
              "in this profile for next time.")
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
    run_p.add_argument("--hot", action="store_true",
                       help="Force the fast on-sale polling cadence (still clamped to robots.txt)")
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

    robots_p = sub.add_parser(
        "robots-check",
        help="Show what each venue's robots.txt permits and the resulting poll interval",
    )
    robots_p.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    robots_p.set_defaults(func=cmd_robots_check)

    prearm_p = sub.add_parser(
        "prearm",
        help="Open the official booking page in your own logged-in browser (optionally at a set time)",
    )
    prearm_p.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    prearm_p.add_argument("--venue", default="science_museum_imax",
                          help="Venue id whose booking_url to open")
    prearm_p.add_argument("--url", help="Open this URL instead of the venue's configured booking_url")
    prearm_p.add_argument("--at", help="Wait until this time first: HH:MM or an ISO datetime")
    prearm_p.add_argument("--tz", default="Europe/London",
                          help="Timezone that --at is expressed in (default Europe/London, "
                               "because venues announce on-sale times in UK time)")
    prearm_p.add_argument("--profile-dir", help="Chromium profile directory to reuse (holds your login)")
    prearm_p.add_argument("--hold-seconds", type=int, default=0,
                          help="Close after N seconds instead of waiting for you to close the window")
    prearm_p.add_argument("--dry-run", action="store_true",
                          help="Print what would happen without launching a browser")
    prearm_p.set_defaults(func=cmd_prearm)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
