from __future__ import annotations

import os

import pytest
import yaml

from dune_watch.config import ConfigError, load_config

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
