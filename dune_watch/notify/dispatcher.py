"""Fans an alert out to every enabled notification channel independently - one
channel's failure never blocks the others - and is the single interception point
for dry-run mode."""
from __future__ import annotations

import logging
import time
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


# Retries for alerts worth retrying. SMTP and ntfy both fail transiently - observed
# "Connection unexpectedly closed: [Errno 54]" from Gmail mid-run - and for a one-shot
# on-sale a dropped notification is the whole failure mode this tool exists to prevent.
# INFO alerts are not retried: the daily status will come round again tomorrow.
RETRY_FROM_URGENCY = ("HIGH", "CRITICAL")
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0


class Dispatcher:
    def __init__(self, channels: list[NotificationChannel], dry_run: bool = False, browser_opener=None):
        self.dry_run = dry_run
        self.channels = [LoggingChannel(c) for c in channels] if dry_run else channels
        self.browser_opener = browser_opener

    def dispatch(self, alert: Alert) -> DispatchResult:
        attempts = RETRY_ATTEMPTS if alert.urgency in RETRY_FROM_URGENCY else 1
        results: dict[str, str] = {}
        for channel in self.channels:
            results[channel.name] = self._send_with_retries(channel, alert, attempts)

        failed = [name for name, status in results.items() if status != "success"]
        if failed and alert.urgency in RETRY_FROM_URGENCY:
            logger.error(
                "ALERT MAY HAVE BEEN MISSED: %s could not be delivered via %s (%s)",
                alert.urgency, ", ".join(failed), alert.title,
            )

        if not self.dry_run and self.browser_opener is not None:
            self.browser_opener.maybe_open(alert)

        return DispatchResult(alert=alert, channel_results=results)

    def _send_with_retries(self, channel, alert: Alert, attempts: int) -> str:
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                channel.send(alert)
                if attempt > 1:
                    logger.info("%s succeeded on attempt %d", channel.name, attempt)
                return "success"
            except NotificationSendError as exc:
                last_error = str(exc)
                logger.warning("%s failed (attempt %d/%d): %s",
                               channel.name, attempt, attempts, exc)
            except Exception as exc:  # a channel's own bug must never take down the others
                last_error = str(exc)
                logger.exception("%s raised an unexpected error (attempt %d/%d)",
                                 channel.name, attempt, attempts)
            if attempt < attempts:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        return f"failed: {last_error}"


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
