from __future__ import annotations

import os

import pytest
import yaml

from pathlib import Path

from dune_watch.adapters.registry import ADAPTER_REGISTRY
from dune_watch.config import ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE_CONFIG = {
    "film": {
        "title": "Dune: Part Three",
        "keywords": ["dune"],
        "format_keywords": ["imax 70mm", "70mm", "imax"],
        "opening_window": {"start": "2026-12-01", "end": "2027-02-28"},
    },
    "polling": {"default_interval_minutes": 20},
    "venues": [
        {
            "id": "science_museum_imax", "name": "Science Museum IMAX",
            "enabled": True, "venue_type": "html_page_diff",
            "url": "https://example.org/dune",
        }
    ],
    "state": {"db_path": "./state.db"},
    "notifications": {"channels": {}},
}


def write_config(tmp_path, config: dict):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_loads_valid_config(tmp_path):
    path = write_config(tmp_path, BASE_CONFIG)
    app_config = load_config(path)
    assert app_config.film.title == "Dune: Part Three"
    assert len(app_config.venues) == 1
    assert app_config.enabled_venues()[0].id == "science_museum_imax"


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does_not_exist.yaml")


def test_missing_required_field_raises(tmp_path):
    config = {k: v for k, v in BASE_CONFIG.items() if k != "film"}
    path = write_config(tmp_path, config)
    with pytest.raises(ConfigError):
        load_config(path)


def test_no_venues_raises(tmp_path):
    config = {**BASE_CONFIG, "venues": []}
    path = write_config(tmp_path, config)
    with pytest.raises(ConfigError):
        load_config(path)


def test_duplicate_venue_id_raises(tmp_path):
    venue = BASE_CONFIG["venues"][0]
    config = {**BASE_CONFIG, "venues": [venue, dict(venue)]}
    path = write_config(tmp_path, config)
    with pytest.raises(ConfigError):
        load_config(path)


def test_enabled_channel_missing_env_var_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("DUNE_WATCH_NTFY_TOPIC", raising=False)
    config = {**BASE_CONFIG, "notifications": {
        "channels": {"ntfy": {"enabled": True, "topic_env_var": "DUNE_WATCH_NTFY_TOPIC"}}
    }}
    path = write_config(tmp_path, config)
    with pytest.raises(ConfigError):
        load_config(path)


def test_env_var_present_allows_load(tmp_path, monkeypatch):
    monkeypatch.setenv("DUNE_WATCH_NTFY_TOPIC", "my-topic")
    config = {**BASE_CONFIG, "notifications": {
        "channels": {"ntfy": {"enabled": True, "topic_env_var": "DUNE_WATCH_NTFY_TOPIC"}}
    }}
    path = write_config(tmp_path, config)
    app_config = load_config(path)
    assert app_config.notifications.channels["ntfy"].enabled is True


def test_secrets_file_sets_env_without_overriding_real_env(tmp_path, monkeypatch):
    monkeypatch.delenv("DUNE_WATCH_NTFY_TOPIC", raising=False)
    secrets_path = tmp_path / "secrets.env"
    secrets_path.write_text("DUNE_WATCH_NTFY_TOPIC=from-file\n")
    config = {**BASE_CONFIG, "secrets_file": str(secrets_path), "notifications": {
        "channels": {"ntfy": {"enabled": True, "topic_env_var": "DUNE_WATCH_NTFY_TOPIC"}}
    }}
    path = write_config(tmp_path, config)
    load_config(path)
    assert os.environ["DUNE_WATCH_NTFY_TOPIC"] == "from-file"


def test_real_env_var_wins_over_secrets_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DUNE_WATCH_NTFY_TOPIC", "from-real-env")
    secrets_path = tmp_path / "secrets.env"
    secrets_path.write_text("DUNE_WATCH_NTFY_TOPIC=from-file\n")
    config = {**BASE_CONFIG, "secrets_file": str(secrets_path), "notifications": {
        "channels": {"ntfy": {"enabled": True, "topic_env_var": "DUNE_WATCH_NTFY_TOPIC"}}
    }}
    path = write_config(tmp_path, config)
    load_config(path)
    assert os.environ["DUNE_WATCH_NTFY_TOPIC"] == "from-real-env"


def test_imap_venue_requires_imap_env_vars(tmp_path, monkeypatch):
    for var in ("DUNE_WATCH_IMAP_HOST", "DUNE_WATCH_IMAP_PORT", "DUNE_WATCH_IMAP_USER", "DUNE_WATCH_IMAP_PASS"):
        monkeypatch.delenv(var, raising=False)
    config = {**BASE_CONFIG, "venues": [
        {"id": "bfi_imax", "name": "BFI IMAX", "enabled": True, "venue_type": "imap_newsletter"}
    ]}
    path = write_config(tmp_path, config)
    with pytest.raises(ConfigError):
        load_config(path)


def test_disabled_imap_venue_does_not_require_env_vars(tmp_path, monkeypatch):
    for var in ("DUNE_WATCH_IMAP_HOST", "DUNE_WATCH_IMAP_USER", "DUNE_WATCH_IMAP_PASS"):
        monkeypatch.delenv(var, raising=False)
    config = {**BASE_CONFIG, "venues": [
        {"id": "bfi_imax", "name": "BFI IMAX", "enabled": False, "venue_type": "imap_newsletter"}
    ]}
    path = write_config(tmp_path, config)
    app_config = load_config(path)  # should not raise
    assert app_config.enabled_venues() == []


# --- The shipped config files must actually load -----------------------------------
# These are the files the deployments run from. A YAML slip in one of them breaks the
# watcher at the worst possible moment, and nothing else in the suite would notice.

SHIPPED_CONFIGS = [
    "config/config.yaml",
    "config/config.github.yaml",
    "config/config.example.yaml",
]


@pytest.mark.parametrize("path", SHIPPED_CONFIGS)
def test_shipped_config_loads(path, monkeypatch):
    monkeypatch.setenv("DUNE_WATCH_NTFY_TOPIC", "test-topic")
    monkeypatch.setenv("DUNE_WATCH_SMTP_USER", "test@example.org")
    monkeypatch.setenv("DUNE_WATCH_SMTP_PASS", "test-pass")
    monkeypatch.setenv("DUNE_WATCH_IMAP_HOST", "imap.example.org")
    monkeypatch.setenv("DUNE_WATCH_IMAP_USER", "test@example.org")
    monkeypatch.setenv("DUNE_WATCH_IMAP_PASS", "test-pass")

    config = load_config(REPO_ROOT / path)
    assert config.venues, f"{path} defines no venues"
    assert config.film.opening_window_start < config.film.opening_window_end

    for venue in config.venues:
        assert venue.venue_type in ADAPTER_REGISTRY, (
            f"{path}: venue {venue.id!r} uses unknown venue_type {venue.venue_type!r}"
        )


@pytest.mark.parametrize("path", SHIPPED_CONFIGS)
def test_shipped_config_opening_window_covers_the_preview_screenings(path, monkeypatch):
    """Both London venues run previews from Tue 15 Dec, three days before the 18 Dec
    general release. An opening_window starting after that silently discards those
    showtimes in html_page_diff."""
    monkeypatch.setenv("DUNE_WATCH_IMAP_HOST", "imap.example.org")
    monkeypatch.setenv("DUNE_WATCH_IMAP_USER", "test@example.org")
    monkeypatch.setenv("DUNE_WATCH_IMAP_PASS", "test-pass")
    monkeypatch.setenv("DUNE_WATCH_NTFY_TOPIC", "test-topic")
    monkeypatch.setenv("DUNE_WATCH_SMTP_USER", "test@example.org")
    monkeypatch.setenv("DUNE_WATCH_SMTP_PASS", "test-pass")

    config = load_config(REPO_ROOT / path)
    assert config.film.opening_window_start <= "2026-12-15"


def test_enabled_imap_venues_use_domain_fragments_not_full_addresses(monkeypatch):
    """The Sept 2026 miss was a sender_filter written as a full address: the filter is a
    substring test, and "news@bfi.org.uk" is not a substring of the real sender
    "noreply@news.bfi.org.uk"."""
    monkeypatch.setenv("DUNE_WATCH_IMAP_HOST", "imap.example.org")
    monkeypatch.setenv("DUNE_WATCH_IMAP_USER", "test@example.org")
    monkeypatch.setenv("DUNE_WATCH_IMAP_PASS", "test-pass")
    monkeypatch.setenv("DUNE_WATCH_NTFY_TOPIC", "test-topic")
    monkeypatch.setenv("DUNE_WATCH_SMTP_USER", "test@example.org")
    monkeypatch.setenv("DUNE_WATCH_SMTP_PASS", "test-pass")

    for path in SHIPPED_CONFIGS:
        config = load_config(REPO_ROOT / path)
        for venue in config.venues:
            if venue.venue_type != "imap_newsletter" or not venue.enabled:
                continue
            for pattern in venue.extra.get("imap", {}).get("sender_filter", []):
                assert "@" not in pattern, (
                    f"{path}: venue {venue.id!r} sender_filter {pattern!r} contains '@'; "
                    "use a bare domain fragment so subdomain senders still match"
                )
