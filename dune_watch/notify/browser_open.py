"""Opens the booking link for high-urgency alerts as a convenience. This is read-only
navigation only - it never fills forms, enters payment details, or completes a
purchase. That boundary is intentional and should not be worked around."""
from __future__ import annotations

import logging
import webbrowser

from dune_watch.models import Alert

logger = logging.getLogger("dune_watch.notify.browser_open")

URGENCY_ORDER = {"INFO": 0, "HIGH": 1, "CRITICAL": 2}


class BrowserOpener:
    def __init__(self, enabled: bool, min_urgency: str):
        self.enabled = enabled
        self.min_urgency = min_urgency

    def maybe_open(self, alert: Alert) -> None:
        if not self.enabled or not alert.booking_link:
            return
        if URGENCY_ORDER.get(alert.urgency, 0) < URGENCY_ORDER.get(self.min_urgency, 1):
            return
        try:
            webbrowser.open(alert.booking_link)
        except Exception as exc:
            logger.warning("Failed to auto-open booking link: %s", exc)
