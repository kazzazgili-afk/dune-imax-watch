from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from dune_watch.cli import _parse_prearm_at, _resolve_prearm_url
from dune_watch.config import ConfigError, load_config
from tests.test_config import REPO_ROOT

LONDON = ZoneInfo("Europe/London")


def test_hhmm_defaults_to_london_not_the_local_machine():
    """Venues announce on-sale times in UK time. If HH:MM silently meant *this
    machine's* zone, a user on UTC+3 pre-arming for "09:00" would arrive at 07:00 London
    - or, on the other side of UTC, two hours after the queue randomised."""
    target = _parse_prearm_at("09:00", "Europe/London")
    assert target.astimezone(LONDON).hour == 9


def test_explicit_timezone_is_honoured():
    target = _parse_prearm_at("09:00", "Asia/Jerusalem")
    assert target.astimezone(ZoneInfo("Asia/Jerusalem")).hour == 9
    # Same instant, different wall clock in London.
    assert target.astimezone(LONDON).hour != 9


def test_hhmm_rolls_to_tomorrow_when_already_past():
    now_london = datetime.now(LONDON)
    earlier = (now_london - timedelta(hours=2)).strftime("%H:%M")
    target = _parse_prearm_at(earlier, "Europe/London")
    assert target > now_london


def test_iso_datetime_with_offset_is_used_verbatim():
    target = _parse_prearm_at("2026-11-04T09:00:00+00:00", "Europe/London")
    assert target.isoformat() == "2026-11-04T09:00:00+00:00"


def test_naive_iso_datetime_takes_the_given_timezone():
    target = _parse_prearm_at("2026-11-04T09:00:00", "Europe/London")
    assert target.utcoffset() is not None
    assert target.astimezone(LONDON).hour == 9


@pytest.mark.parametrize("bad", ["not-a-time", "25:99:99:00", ""])
def test_unparseable_time_is_a_config_error(bad):
    with pytest.raises(ConfigError, match="Could not parse"):
        _parse_prearm_at(bad, "Europe/London")


def test_unknown_timezone_is_a_config_error():
    with pytest.raises(ConfigError, match="Unknown timezone"):
        _parse_prearm_at("09:00", "Mars/Olympus")


def test_prearm_url_prefers_booking_url_over_the_watched_page(monkeypatch):
    """The page we *poll* is the marketing page; the page we *open* must be the booking
    front door, because that is what redirects into the official waiting room."""
    for key in ("DUNE_WATCH_IMAP_HOST", "DUNE_WATCH_IMAP_USER", "DUNE_WATCH_IMAP_PASS"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("DUNE_WATCH_NTFY_TOPIC", "x")
    monkeypatch.setenv("DUNE_WATCH_SMTP_USER", "x")
    monkeypatch.setenv("DUNE_WATCH_SMTP_PASS", "x")

    config = load_config(REPO_ROOT / "config/config.yaml")
    url = _resolve_prearm_url(config, "science_museum_imax")
    assert "my.sciencemuseum.org.uk" in url
    assert "see-and-do" not in url


def test_prearm_url_errors_clearly_for_a_venue_with_nothing_to_open(monkeypatch):
    for key in ("DUNE_WATCH_IMAP_HOST", "DUNE_WATCH_IMAP_USER", "DUNE_WATCH_IMAP_PASS"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("DUNE_WATCH_NTFY_TOPIC", "x")
    monkeypatch.setenv("DUNE_WATCH_SMTP_USER", "x")
    monkeypatch.setenv("DUNE_WATCH_SMTP_PASS", "x")

    config = load_config(REPO_ROOT / "config/config.yaml")
    with pytest.raises(ConfigError, match="no 'booking_url'"):
        _resolve_prearm_url(config, "bfi_imax")
    with pytest.raises(ConfigError, match="No venue with id"):
        _resolve_prearm_url(config, "nope")
