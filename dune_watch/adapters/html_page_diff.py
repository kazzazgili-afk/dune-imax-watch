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
from dune_watch.util.robots import get_cache
from dune_watch.util.text import first_match, matches_any

logger = logging.getLogger("dune_watch.adapters.html_page_diff")

# Anchor text that marks a real booking call-to-action.
DEFAULT_BOOKING_TEXT_MARKERS = (
    "check ticket availability", "book tickets", "book now", "buy tickets", "get tickets",
)
# Hrefs that look like a booking link. For the Science Museum the tell is the
# Tessitura calendar app with a production id: my.sciencemuseum.org.uk/events?...kid=794
DEFAULT_BOOKING_LINK_PATTERNS = ("/events?", "kid=")
# ...but the same host also serves account pages ("Newsletter", "Sign up", "My
# bookings"), and tickets.sciencemuseum.org.uk sells general museum admission. Treating
# any of those as the film's booking link would fire a permanent false on-sale.
DEFAULT_BOOKING_LINK_EXCLUDES = (
    "/account/", "/login", "/basket", "/cart", "account/create",
)
# A page can advertise a booking CTA and still be sold out - the live Odyssey page does
# exactly that - so sold-out wording has to outrank the presence of a link.
DEFAULT_SOLD_OUT_MARKERS = ("sold out", "no longer available", "fully booked")
# Scope everything to the film's own content block. The rest of the page carries an
# "Other things to see and do" carousel whose cards are giant anchors containing phrases
# like "book tickets" and "sell-out", and whose contents rotate - which both produced
# false booking links and a fingerprint that changed on its own, emitting a steady
# drip of INFO "Listing updated" alerts with nothing behind them.
DEFAULT_CONTENT_SELECTOR = "main, [role=main], article"
# A call to action is a short label ("Check ticket availability"), never a paragraph.
MAX_CTA_TEXT_LENGTH = 80

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

        # Enforced, not advisory: never fetch a path the site tells us to stay out of.
        verdict = get_cache(self.app_config.polling.user_agent).check(url)
        if not verdict.allowed:
            raise AdapterFetchError(
                f"robots.txt disallows {url} for this User-Agent - refusing to fetch"
            )

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

        parser_hints = self.venue_config.extra.get("parser_hints", {})
        content = self._content_root(soup, parser_hints)

        page_text = content.get_text(separator=" ", strip=True)
        page_text_lower = page_text.lower()
        fingerprint = hashlib.sha256(page_text.encode("utf-8")).hexdigest()

        if not matches_any(page_text, self.app_config.film.keywords):
            return []

        page_format = self._match_format(page_text_lower)

        selector = parser_hints.get("showtime_container_selector")
        showtime_elements = []
        if selector:
            try:
                showtime_elements = content.select(selector)
            except Exception as exc:
                logger.warning("Selector '%s' invalid for venue %s: %s", selector, self.venue_config.id, exc)
                showtime_elements = []

        if not showtime_elements:
            # No per-showtime markup: this venue keeps showtimes inside a separate
            # booking app, so the page itself is one listing whose state is told by
            # whether a booking call-to-action has appeared yet.
            booking_link = self._find_booking_link(content, parser_hints)
            availability = self._page_availability(page_text_lower, booking_link, parser_hints)
            return [self._build_listing(
                show_date=None, show_time=None, format_label=page_format,
                availability=availability, booking_link=booking_link, fingerprint=fingerprint,
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

    def _content_root(self, soup, parser_hints: dict):
        """The film's own content block, or the whole document if the page has none."""
        selector = parser_hints.get("content_selector", DEFAULT_CONTENT_SELECTOR)
        if not selector:
            return soup
        try:
            match = soup.select_one(selector)
        except Exception as exc:
            logger.warning("content_selector '%s' invalid for venue %s: %s",
                           selector, self.venue_config.id, exc)
            return soup
        if match is None:
            logger.debug("content_selector '%s' matched nothing for venue %s; using whole page",
                         selector, self.venue_config.id)
            return soup
        return match

    def _find_booking_link(self, soup, parser_hints: dict) -> Optional[str]:
        """The film's booking CTA, identified by anchor text first and href shape second.

        Anchor text is the stronger signal: the Science Museum's real CTA reads "Check
        ticket availability" and is the only booking-ish anchor on the page that is not
        an account or general-admission link.
        """
        text_markers = [
            m.lower() for m in parser_hints.get("booking_text_markers", DEFAULT_BOOKING_TEXT_MARKERS)
        ]
        href_patterns = [
            p.lower() for p in parser_hints.get("booking_link_patterns", DEFAULT_BOOKING_LINK_PATTERNS)
        ]
        excludes = [
            e.lower() for e in parser_hints.get("booking_link_excludes", DEFAULT_BOOKING_LINK_EXCLUDES)
        ]

        candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(exc in href.lower() for exc in excludes):
                continue
            candidates.append((a.get_text(separator=" ", strip=True).lower(), href))

        for text, href in candidates:
            if not text or len(text) > MAX_CTA_TEXT_LENGTH:
                continue
            if any(marker in text for marker in text_markers):
                return href
        for text, href in candidates:
            if any(pattern in href.lower() for pattern in href_patterns):
                return href
        return None

    def _page_availability(
        self, page_text_lower: str, booking_link: Optional[str], parser_hints: dict
    ) -> str:
        """Precedence: sold-out wording, then a booking CTA, then register-interest.

        Sold-out wording has to win, otherwise the Odyssey page - which is sold out but
        still links to its calendar - would read as bookable forever. Register-interest
        has to lose to a booking CTA, because venues keep the register-interest block on
        the page for later dates after the first batch goes on sale; that co-existence is
        what made the original 'register_interest wins' logic miss an on-sale entirely.
        """
        sold_out_markers = [
            m.lower() for m in parser_hints.get("sold_out_markers", DEFAULT_SOLD_OUT_MARKERS)
        ]
        register_markers = [m.lower() for m in parser_hints.get("register_interest_markers", [])]

        if any(m in page_text_lower for m in sold_out_markers):
            return "sold_out"
        if booking_link:
            return "bookable"
        if any(m in page_text_lower for m in register_markers):
            return "register_interest"
        return "unknown"

    def _match_format(self, text_lower: str) -> Optional[str]:
        return first_match(text_lower, self.app_config.film.format_keywords)

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
