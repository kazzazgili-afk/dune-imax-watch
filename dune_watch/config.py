"""Loads config.yaml, resolves secrets from environment variables (or a gitignored
secrets file), and validates that anything enabled has what it needs to actually run."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


class ConfigError(Exception):
    """Raised for structurally invalid config or missing required secrets."""


@dataclass
class FilmConfig:
    title: str
    keywords: list[str]
    format_keywords: list[str]
    opening_window_start: str
    opening_window_end: str


@dataclass
class PollingConfig:
    default_interval_minutes: int = 20
    jitter_seconds: int = 90
    user_agent: str = "dune-imax-watch/0.1"
    http_timeout_seconds: int = 15
    http_max_retries: int = 2
    failing_source_alert_after_cycles: int = 6
    batch_threshold: int = 3


@dataclass
class VenueConfig:
    id: str
    name: str
    enabled: bool
    venue_type: str
    poll_interval_minutes: int
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelConfig:
    name: str
    enabled: bool
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class NotificationsConfig:
    channels: dict[str, ChannelConfig]
    auto_open_enabled: bool
    auto_open_min_urgency: str


@dataclass
class AppConfig:
    film: FilmConfig
    polling: PollingConfig
    venues: list[VenueConfig]
    state_db_path: str
    notifications: NotificationsConfig

    def enabled_venues(self) -> list[VenueConfig]:
        return [v for v in self.venues if v.enabled]

    def venue_by_id(self, venue_id: str) -> Optional[VenueConfig]:
        for v in self.venues:
            if v.id == venue_id:
                return v
        return None


REQUIRED_CHANNEL_ENV_FIELDS = {
    "ntfy": ["topic_env_var"],
    "email_smtp": ["username_env_var", "password_env_var"],
}

REQUIRED_IMAP_ENV_VARS = (
    "DUNE_WATCH_IMAP_HOST",
    "DUNE_WATCH_IMAP_USER",
    "DUNE_WATCH_IMAP_PASS",
)


def _load_secrets_file(secrets_path: Path) -> None:
    if not secrets_path.exists():
        return
    for line in secrets_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # real environment variables always win over the secrets file
        os.environ.setdefault(key.strip(), value.strip())


def _resolve_env(name: str, required: bool, context: str) -> Optional[str]:
    value = os.environ.get(name)
    if required and not value:
        raise ConfigError(f"Missing required environment variable '{name}' for {context}")
    return value


def _check_channel_secrets(name: str, settings: dict[str, Any]) -> None:
    for field_name in REQUIRED_CHANNEL_ENV_FIELDS.get(name, []):
        env_var = settings.get(field_name)
        if env_var:
            _resolve_env(env_var, required=True, context=f"notifications.channels.{name}")


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text()) or {}

    secrets_file = raw.get("secrets_file")
    if secrets_file:
        _load_secrets_file(Path(secrets_file))

    try:
        film_raw = raw["film"]
        film = FilmConfig(
            title=film_raw["title"],
            keywords=list(film_raw.get("keywords", [])),
            format_keywords=list(film_raw.get("format_keywords", [])),
            opening_window_start=film_raw["opening_window"]["start"],
            opening_window_end=film_raw["opening_window"]["end"],
        )

        polling_raw = raw.get("polling", {})
        polling = PollingConfig(**{
            k: v for k, v in polling_raw.items() if k in PollingConfig.__dataclass_fields__
        })

        venues: list[VenueConfig] = []
        for v in raw.get("venues", []):
            reserved = {"id", "name", "enabled", "venue_type", "poll_interval_minutes"}
            venues.append(VenueConfig(
                id=v["id"],
                name=v["name"],
                enabled=bool(v.get("enabled", False)),
                venue_type=v["venue_type"],
                poll_interval_minutes=int(v.get("poll_interval_minutes", polling.default_interval_minutes)),
                extra={k: val for k, val in v.items() if k not in reserved},
            ))
        if not venues:
            raise ConfigError("Config must define at least one venue under 'venues:'")

        venue_ids = [v.id for v in venues]
        if len(venue_ids) != len(set(venue_ids)):
            raise ConfigError("Duplicate venue id found in config 'venues:' list")

        imap_venues = [v for v in venues if v.enabled and v.venue_type == "imap_newsletter"]
        if imap_venues:
            context = "imap_newsletter venue(s): " + ", ".join(v.id for v in imap_venues)
            for env_name in REQUIRED_IMAP_ENV_VARS:
                _resolve_env(env_name, required=True, context=context)

        state_raw = raw.get("state", {})
        state_db_path = state_raw.get("db_path", "./state.db")

        notif_raw = raw.get("notifications", {})
        channels_raw = notif_raw.get("channels", {})
        channels: dict[str, ChannelConfig] = {}
        for cname, csettings in channels_raw.items():
            csettings = csettings or {}
            channel = ChannelConfig(
                name=cname,
                enabled=bool(csettings.get("enabled", False)),
                settings={k: v for k, v in csettings.items() if k != "enabled"},
            )
            if channel.enabled:
                _check_channel_secrets(cname, channel.settings)
            channels[cname] = channel

        auto_open_raw = notif_raw.get("auto_open_booking_link", {})
        notifications = NotificationsConfig(
            channels=channels,
            auto_open_enabled=bool(auto_open_raw.get("enabled", False)),
            auto_open_min_urgency=auto_open_raw.get("min_urgency", "HIGH"),
        )
    except KeyError as exc:
        raise ConfigError(f"Missing required config field: {exc}") from exc

    return AppConfig(
        film=film,
        polling=polling,
        venues=venues,
        state_db_path=state_db_path,
        notifications=notifications,
    )
