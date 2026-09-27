"""ntfy.sh push channel.

Two things matter for an on-sale alert beyond "a notification appeared":

1. It must be one tap to the booking page. ntfy's `Click` header makes tapping the
   notification open the URL, and `Actions` adds an explicit button, so there is no
   hunting for a link on a phone.
2. A CRITICAL must survive being missed. ntfy's `Delay` header schedules delivery on
   the *server*, so follow-up nudges still arrive even if this process has since died
   or the machine has gone to sleep - which is exactly the failure mode that took out
   every channel at once in the logs.
"""
from __future__ import annotations

import logging
import os

import requests

from dune_watch.models import Alert
from dune_watch.notify.base import NotificationSendError

logger = logging.getLogger("dune_watch.notify.ntfy")

PRIORITY_MAP = {"INFO": "default", "HIGH": "high", "CRITICAL": "urgent"}
TAGS_MAP = {
    "INFO": "movie_camera",
    "HIGH": "movie_camera,bell",
    "CRITICAL": "rotating_light,movie_camera",
}
# ntfy rejects delays under 10 seconds.
MIN_DELAY_SECONDS = 10


class NtfyChannel:
    name = "ntfy"

    def __init__(self, settings: dict):
        self.server = settings.get("server", "https://ntfy.sh")
        topic_env_var = settings.get("topic_env_var")
        self.topic = os.environ.get(topic_env_var) if topic_env_var else None
        # Follow-up nudges for CRITICAL alerts, in minutes after the first push.
        self.repeat_after_minutes = list(settings.get("repeat_critical_after_minutes", [3, 10]))

    def send(self, alert: Alert) -> None:
        if not self.topic:
            raise NotificationSendError("ntfy topic not configured (missing env var)")

        self._post(alert, delay_minutes=None)

        if alert.urgency == "CRITICAL":
            for minutes in self.repeat_after_minutes:
                try:
                    self._post(alert, delay_minutes=minutes)
                except NotificationSendError as exc:
                    # A failed reminder must never mask a delivered first alert.
                    logger.warning("ntfy follow-up at +%smin failed: %s", minutes, exc)

    def _post(self, alert: Alert, delay_minutes) -> None:
        url = f"{self.server.rstrip('/')}/{self.topic}"
        headers = {
            "Title": alert.title,
            "Priority": PRIORITY_MAP.get(alert.urgency, "default"),
            "Tags": TAGS_MAP.get(alert.urgency, "movie_camera"),
        }
        if alert.booking_link:
            headers["Click"] = alert.booking_link
            headers["Actions"] = f"view, Open booking page, {alert.booking_link}"
        if delay_minutes:
            seconds = max(int(delay_minutes * 60), MIN_DELAY_SECONDS)
            headers["Delay"] = f"{seconds}s"

        body = alert.body
        if delay_minutes:
            body = f"[reminder +{delay_minutes}min] {body}"

        try:
            response = requests.post(
                url, data=body.encode("utf-8"), headers=headers, timeout=10
            )
            response.raise_for_status()
        except Exception as exc:
            raise NotificationSendError(f"ntfy POST failed: {exc}") from exc
