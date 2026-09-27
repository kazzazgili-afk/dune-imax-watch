"""Opens a real, logged-in browser at the venue's official booking page and stops.

Why this exists, and why it is deliberately unclever:

The Science Museum's booking app sits behind Queue-it. Queue-it's pre-queue collects
everyone who arrives before the sale starts and then **randomises them like a raffle**
when the countdown hits zero; only people who arrive after that are served
first-come-first-served, at the back. So being fast at the moment of the drop wins
nothing. Being *already in the waiting room* before zero is the only thing that
improves your odds, and this module exists to guarantee that happens on time.

Queue-it also runs Akamai Bot Manager during the pre-queue phase, and sessions it marks
"Aggressive" are hard-blocked or sent to the back of the line at queue start. That makes
the usual bot toolkit actively counterproductive here, so this module contains:

  - no stealth or fingerprint patching        - no proxies
  - no CAPTCHA solving                        - no automated payment
  - no multiple sessions or queue entries     - no refreshing of the queue page

It is an ordinary Chromium holding the user's own session, driven to one URL, that then
hands control to the human. Refreshing matters: reloading a Queue-it page can forfeit
your place, so once we have landed we do not touch the page again.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("dune_watch.assist.warm_browser")

DEFAULT_PROFILE_DIR = Path.home() / ".dune-watch" / "browser-profile"

QUEUE_HOST_MARKERS = ("queue-it.net", "queue-it.com")


class WarmBrowserError(Exception):
    """Playwright missing, profile unusable, or navigation failed."""


@dataclass
class ArrivalReport:
    url: str
    in_queue: bool
    signed_in: Optional[bool]
    note: str

    def describe(self) -> str:
        where = "in the official waiting room" if self.in_queue else "on the booking page"
        who = {True: "signed in", False: "NOT signed in", None: "sign-in state unknown"}[self.signed_in]
        return f"Landed {where}, {who}. {self.note}".strip()


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on local install
        raise WarmBrowserError(
            "Playwright is not installed. It is an optional extra so the headless poller "
            "stays lightweight:\n"
            "  .venv/bin/pip install -r requirements-assist.txt\n"
            "  .venv/bin/python -m playwright install chromium"
        ) from exc
    return sync_playwright


def wait_until(target: datetime, poll_seconds: float = 1.0) -> None:
    """Block until `target`. Kept separate so `prearm` can be tested without sleeping."""
    while True:
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        if remaining > 60 and int(remaining) % 60 == 0:
            logger.info("Pre-arm in %d min", int(remaining // 60))
        time.sleep(min(poll_seconds, remaining))


def open_and_hold(
    url: str,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    hold_seconds: Optional[int] = None,
    signed_in_markers: tuple[str, ...] = ("my bookings", "sign out", "log out", "my account"),
) -> ArrivalReport:
    """Open `url` in a persistent, headful Chromium and leave it open for the human.

    `hold_seconds=None` means hold until the user closes the window, which is the normal
    mode: they take over and complete the booking themselves.
    """
    sync_playwright = _require_playwright()
    profile_dir = Path(profile_dir).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,          # a real window, for a real person
                viewport=None,           # use the OS window size
                args=["--start-maximized"],
            )
        except Exception as exc:
            raise WarmBrowserError(f"Could not launch Chromium profile at {profile_dir}: {exc}") from exc

        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:
                raise WarmBrowserError(f"Navigation to {url} failed: {exc}") from exc

            page.bring_to_front()
            landed = page.url
            in_queue = any(marker in landed.lower() for marker in QUEUE_HOST_MARKERS)

            signed_in: Optional[bool] = None
            try:
                body = (page.inner_text("body") or "").lower()
                signed_in = any(marker in body for marker in signed_in_markers)
            except Exception:
                signed_in = None

            note = (
                "Do not reload this page - refreshing a waiting room can forfeit your place."
                if in_queue else
                "Booking page is open and ready; complete seats and payment yourself."
            )
            report = ArrivalReport(url=landed, in_queue=in_queue, signed_in=signed_in, note=note)
            logger.info(report.describe())

            # Hold the window. The point is to hand a live session to the human, so we
            # never navigate, click or refresh from here on.
            if hold_seconds is None:
                _hold_until_closed(page)
            else:
                time.sleep(hold_seconds)
            return report
        finally:
            try:
                context.close()
            except Exception:
                pass


def _hold_until_closed(page) -> None:
    logger.info("Browser is yours - close the window when you're done.")
    try:
        while not page.is_closed():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Released by user (Ctrl+C); leaving the browser as-is.")
    except Exception:
        # A closed browser raises in various ways across Playwright versions; any of
        # them just means the human is finished.
        pass
