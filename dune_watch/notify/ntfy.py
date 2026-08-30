from __future__ import annotations

import os

import requests

from dune_watch.models import Alert
from dune_watch.notify.base import NotificationSendError

PRIORITY_MAP = {"INFO": "default", "HIGH": "high", "CRITICAL": "urgent"}


class NtfyChannel:
    name = "ntfy"

    def __init__(self, settings: dict):
        self.server = settings.get("server", "https://ntfy.sh")
        topic_env_var = settings.get("topic_env_var")
        self.topic = os.environ.get(topic_env_var) if topic_env_var else None

    def send(self, alert: Alert) -> None:
        if not self.topic:
            raise NotificationSendError("ntfy topic not configured (missing env var)")
        url = f"{self.server.rstrip('/')}/{self.topic}"
        message = f"{alert.title}\n\n{alert.body}"
        try:
            response = requests.post(
                url,
                data=message.encode("utf-8"),
                headers={
                    "Priority": PRIORITY_MAP.get(alert.urgency, "default"),
                    "Tags": "movie_camera",
                },
                timeout=10,
            )
            response.raise_for_status()
        except Exception as exc:
            raise NotificationSendError(f"ntfy POST failed: {exc}") from exc
