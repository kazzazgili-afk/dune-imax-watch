from __future__ import annotations

from typing import Protocol

from dune_watch.models import Alert


class NotificationSendError(Exception):
    """Raised by a channel when a send attempt fails; caught by the dispatcher so one
    channel's failure never blocks the others."""


class NotificationChannel(Protocol):
    name: str

    def send(self, alert: Alert) -> None:
        ...
