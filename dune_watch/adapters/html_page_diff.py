"""Generic config-driven adapter for a public, non-gated cinema page (e.g. Science
Museum IMAX). Polls the page politely, looks for the film, and either reports a
'register interest' placeholder or parses individual showtime blocks."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from typing import Optional

from bs4 import BeautifulSoup

from dune_watch.adapters.base import Adapter, AdapterFetchError
from dune_watch.models import RawListing
from dune_watch.util.http import build_session

logger = logging.getLogger("dune_watch.adapters.html_page_diff")

_MONTHS = {
    name.lower(): i
    for i, name in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}
_DATE_PATTERN = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})\b", re.IGNORECASE
)
_TIME_PATTERN = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


def _extract_date(text: str) -> Optional[str]:
    match = _DATE_PATTERN.search(text)
    if not match:
        return None
    day, month_name, year = match.groups()
    try:
        return date(int(year), _MONTHS[month_name.lower()], int(day)).isoformat()
    except ValueError:
        return None


def _extract_time(text: str) -> Optional[str]:
    match = _TIME_PATTERN.search(text)
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


class HtmlPageDiffAdapter(Adapter):
    def fetch(self) -> list[RawListing]:
        url = self.venue_config.extra.get("url")
        if not url:
            raise AdapterFetchError(f"Venue '{self.venue_config.id}' is missing a 'url'")

        session = build_session(self.app_config.polling)
        try:
            response = session.get(url, timeout=self.app_config.polling.http_timeout_seconds)
            response.raise_for_status()
        except Exception as exc:
            raise AdapterFetchError(f"GET {url} failed: {exc}") from exc

        try:
            soup = BeautifulSoup(response.text, "html.parser")
        except Exception as exc:
            raise AdapterFetchError(f"Failed to parse HTML from {url}: {exc}") from exc

        page_text = soup.get_text(separator=" ", strip=True)
        page_text_lower = page_text.lower()
        fingerprint = hashlib.sha256(page_text.encode("utf-8")).hexdigest()

        film_keywords = [k.lower() for k in self.app_config.film.keywords]
        if not any(keyword in page_text_lower for keyword in film_keywords):
            return []

        page_format = self._match_format(page_text_lower)

        parser_hints = self.venue_config.extra.get("parser_hints", {})
        selector = parser_hints.get("showtime_container_selector")
        showtime_elements = []
        if selector:
            try:
                showtime_elements = soup.select(selector)
            except Exception as exc:
                logger.warning("Selector '%s' invalid for venue %s: %s", selector, self.venue_config.id, exc)
                showtime_elements = []

        if not showtime_elements:
            register_markers = [m.lower() for m in parser_hints.get("register_interest_markers", [])]
            availability = "register_interest" if any(m in page_text_lower for m in register_markers) else "unknown"
            return [self._build_listing(
                show_date=None, show_time=None, format_label=page_format,
                availability=availability, booking_link=None, fingerprint=fingerprint,
            )]

        listings: list[RawListing] = []
        for element in showtime_elements:
            try:
                listing = self._parse_showtime_element(element, page_format, fingerprint)
            except Exception as exc:
                logger.warning("Skipping malformed showtime block on %s: %s", url, exc)
                continue
            if listing is not None:
                listings.append(listing)
        return listings

    def _match_format(self, text_lower: str) -> Optional[str]:
        for keyword in self.app_config.film.format_keywords:
            if keyword.lower() in text_lower:
                return keyword
        return None

    def _parse_showtime_element(self, element, page_format: Optional[str], fingerprint: str) -> Optional[RawListing]:
        text = element.get_text(separator=" ", strip=True)
        text_lower = text.lower()

        show_date = _extract_date(text)
        show_time = _extract_time(text)
        format_label = self._match_format(text_lower) or page_format

        link_el = element.find("a", href=True)
        booking_link = link_el["href"] if link_el else None

        if "sold out" in text_lower or "unavailable" in text_lower:
            availability = "sold_out"
        elif booking_link:
            availability = "bookable"
        else:
            availability = "unavailable"

        opening_start = self.app_config.film.opening_window_start
        opening_end = self.app_config.film.opening_window_end
        if show_date is not None and not (opening_start <= show_date <= opening_end):
            return None  # outside the window we care about, e.g. a stale/unrelated screening

        return self._build_listing(
            show_date=show_date, show_time=show_time, format_label=format_label,
            availability=availability, booking_link=booking_link, fingerprint=fingerprint,
        )

    def _build_listing(self, show_date, show_time, format_label, availability, booking_link, fingerprint) -> RawListing:
        return RawListing(
            venue_id=self.venue_config.id,
            venue_name=self.venue_config.name,
            film_title=self.app_config.film.title,
            show_date=show_date,
            show_time=show_time,
            format_label=format_label,
            availability=availability,
            booking_link=booking_link,
            source_type="html_page_diff",
            raw_fingerprint=fingerprint,
        )
