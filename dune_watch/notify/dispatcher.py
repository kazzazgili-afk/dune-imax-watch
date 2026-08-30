"""Fans an alert out to every enabled notification channel independently - one
channel's failure never blocks the others - and is the single interception point
for dry-run mode."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from dune_watch.models import Alert
from dune_watch.notify.base import NotificationChannel, NotificationSendError

logger = logging.getLogger("dune_watch.notify.dispatcher")


@dataclass
class DispatchResult:
    alert: Alert
    channel_results: dict[str, str] = field(default_factory=dict)


class LoggingChannel:
    """Wraps a real channel for dry-run mode: logs what would be sent instead of
    actually sending it."""

    def __init__(self, wrapped: NotificationChannel):
        self.wrapped = wrapped
        self.name = wrapped.name

    def send(self, alert: Alert) -> None:
        logger.info("[DRY RUN] Would send via %s: %s | %s", self.name, alert.title, alert.body)


class Dispatcher:
    def __init__(self, channels: list[NotificationChannel], dry_run: bool = False, browser_opener=None):
        self.dry_run = dry_run
        self.channels = [LoggingChannel(c) for c in channels] if dry_run else channels
        self.browser_opener = browser_opener

    def dispatch(self, alert: Alert) -> DispatchResult:
        results: dict[str, str] = {}
        for channel in self.channels:
            try:
                channel.send(alert)
                results[channel.name] = "success"
            except NotificationSendError as exc:
                logger.warning("%s failed: %s", channel.name, exc)
                results[channel.name] = f"failed: {exc}"
            except Exception as exc:  # a channel's own bug must never take down the others
                logger.exception("%s raised an unexpected error", channel.name)
                results[channel.name] = f"failed: {exc}"

        if not self.dry_run and self.browser_opener is not None:
            self.browser_opener.maybe_open(alert)

        return DispatchResult(alert=alert, channel_results=results)


def build_channels(app_config) -> list[NotificationChannel]:
    from dune_watch.notify.email_smtp import EmailSmtpChannel
    from dune_watch.notify.macos import MacOSNotifyChannel
    from dune_watch.notify.ntfy import NtfyChannel

    channels: list[NotificationChannel] = []
    configured = app_config.notifications.channels
    if configured.get("macos_native") and configured["macos_native"].enabled:
        channels.append(MacOSNotifyChannel())
    if configured.get("ntfy") and configured["ntfy"].enabled:
        channels.append(NtfyChannel(configured["ntfy"].settings))
    if configured.get("email_smtp") and configured["email_smtp"].enabled:
        channels.append(EmailSmtpChannel(configured["email_smtp"].settings))
    return channels
