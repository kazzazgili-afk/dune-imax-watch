"""Reads the user's own mailbox (read-only IMAP) for the official venue on-sale
newsletters the user signed up for (BFI IMAX, Science Museum IMAX). This never touches
a venue's ticketing site - it only reads mail the user already owns.

Matching notes learned the hard way (see tests/test_imap_newsletter_adapter.py):
 - `sender_filter` is a *substring* test against the raw From header, so it must be
   written as a domain fragment ("bfi.org.uk"), not a full address. A filter of
   "news@bfi.org.uk" does NOT match the real sender "noreply@news.bfi.org.uk", and
   "sciencemuseum.org.uk" does NOT match the real sender "no-reply@sciencemuseum.ac.uk".
   Both of those mistakes silently dropped real on-sale emails in Sept 2026.
 - Venue newsletters use playful subject lines ("Head back to Arrakis sooner than
   planned") with the film and format named only in the body, so keywords are matched
   against subject + body together, never the subject alone.
 - Booking links are wrapped in click-trackers (e.g. e.wordfly.com), so a booking link
   is identified by its *anchor text* ("Book tickets for Tuesday 15 December, 14.30")
   rather than by its href domain.
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import os
import re
from datetime import date, timedelta
from email.header import decode_header, make_header
from email.message import Message
from typing import Optional

from bs4 import BeautifulSoup

from dune_watch.adapters.base import Adapter, AdapterFetchError
from dune_watch.models import RawListing
from dune_watch.util.text import first_match, matches_any

logger = logging.getLogger("dune_watch.adapters.imap_newsletter")

BOOKABLE_PHRASES = (
    "on sale", "tickets available", "book now", "buy tickets", "tickets released",
    "book for", "released for", "booking now open",
)
REGISTER_PHRASES = ("coming soon", "sign up", "register your interest", "register interest")

# Transactional mail from the SAME domain as the on-sale newsletter must never be read
# as an announcement. On 28 Sept 2026 an order confirmation from
# confirmation@sciencemuseum.ac.uk ("Thank You for Your Order", "Your tickets for the
# Science Museum") matched the sender filter, the word "tickets" and the film name, and
# fired two "Tickets on sale" alerts for tickets the user had just bought themselves.
# False alarms like that are how a real alert gets ignored.
#
# These markers are receipt-shaped and cannot plausibly appear in a marketing
# announcement, which makes them a safer test than subject wording alone (a genuine
# email could legitimately be titled "Your tickets for Dune are now on sale").
DEFAULT_TRANSACTIONAL_MARKERS = (
    "order number", "total paid", "total due", "your account information",
    "account number", "password reset", "account was updated",
    "thank you for registering", "thank you for your order", "booking reference",
    "this is your receipt", "order confirmation",
)
# Belt and braces: the museum sends announcements from no-reply@ and receipts from
# confirmation@, so the local part is itself a reliable discriminator.
DEFAULT_SENDER_EXCLUDES = ("confirmation@", "noreply-order@", "receipts@", "no-reply-order@")

# Anchor text that marks a real booking link, as opposed to a social/footer link.
DEFAULT_BOOKING_TEXT_MARKERS = (
    "book tickets", "book now", "buy tickets", "check ticket availability",
    "book for", "get tickets",
)

# Links that are never booking links even if their anchor text looks promising.
_SOCIAL_LINK_HINTS = (
    "facebook", "twitter", "instagram", "youtube", "whatsapp", "tripadvisor",
    "bsky", "tiktok", "linkedin", "/privacy", "unsubscribe", "mailto:",
)

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
# UK venues write showtimes as both "19:15" and "19.15", so accept either separator.
_TIME_PATTERN = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")


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


def _decode_header(raw: Optional[str]) -> str:
    """Newsletter subjects are frequently RFC 2047 encoded; decode before matching."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _extract_body_and_anchors(msg: Message) -> tuple[str, list[tuple[str, str]]]:
    """Returns (body_text, anchors) where each anchor is (visible_text, href).

    Anchor text is what lets us tell a booking link apart from a footer link once the
    href has been rewritten by a click-tracker.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if "attachment" in str(part.get("Content-Disposition", "")):
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = payload.decode("utf-8", errors="replace")
            if part.get_content_type() == "text/plain":
                plain_parts.append(text)
            elif part.get_content_type() == "text/html":
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace") if payload else ""
        if msg.get_content_type() == "text/html":
            html_parts.append(text)
        else:
            plain_parts.append(text)

    body_text = "\n".join(plain_parts)
    anchors: list[tuple[str, str]] = []
    for html_part in html_parts:
        soup = BeautifulSoup(html_part, "html.parser")
        if not body_text:
            body_text += " " + soup.get_text(separator=" ", strip=True)
        for a in soup.find_all("a", href=True):
            anchors.append((a.get_text(separator=" ", strip=True), a["href"]))

    if not anchors:
        anchors = _plaintext_anchors(body_text)

    return body_text.strip(), anchors


def _plaintext_anchors(body_text: str) -> list[tuple[str, str]]:
    """Pair each bare URL in a text/plain newsletter with the text that introduces it.

    Plain-text newsletters put the call to action immediately above the link
    ("Book tickets for Tuesday 15 December, 14.30\\nhttps://..."), so that paragraph is
    the closest equivalent of anchor text and is what identifies a booking link.
    """
    anchors: list[tuple[str, str]] = []
    cursor = 0
    for match in _URL_PATTERN.finditer(body_text):
        preceding = body_text[cursor:match.start()]
        context = re.split(r"\n\s*\n", preceding)[-1].strip()
        anchors.append((context[-200:], match.group(0)))
        cursor = match.end()
    return anchors


def _is_social_or_footer(href: str) -> bool:
    lowered = href.lower()
    return any(hint in lowered for hint in _SOCIAL_LINK_HINTS)


def _find_booking_anchors(
    anchors: list[tuple[str, str]],
    text_markers: tuple[str, ...],
    href_patterns: tuple[str, ...],
) -> list[tuple[str, str]]:
    """Booking anchors in document order, preferring anchor text over href shape."""
    by_text = [
        (text, href)
        for text, href in anchors
        if text and not _is_social_or_footer(href)
        and any(marker in text.lower() for marker in text_markers)
    ]
    if by_text:
        return by_text
    if href_patterns:
        return [
            (text, href)
            for text, href in anchors
            if not _is_social_or_footer(href)
            and any(pattern in href.lower() for pattern in href_patterns)
        ]
    return []


class ImapNewsletterAdapter(Adapter):
    def fetch(self) -> list[RawListing]:
        imap_conf = self.venue_config.extra.get("imap", {})
        folder = imap_conf.get("folder", "INBOX")
        sender_filter = [s.lower() for s in imap_conf.get("sender_filter", [])]
        subject_keywords = [k.lower() for k in imap_conf.get("subject_keywords", [])]
        lookback_days = int(imap_conf.get("lookback_days_on_first_run", 30))
        text_markers = tuple(
            m.lower() for m in imap_conf.get("booking_text_markers", DEFAULT_BOOKING_TEXT_MARKERS)
        )
        transactional_markers = tuple(
            m.lower() for m in imap_conf.get("transactional_markers", DEFAULT_TRANSACTIONAL_MARKERS)
        )
        sender_excludes = tuple(
            s.lower() for s in imap_conf.get("sender_exclude", DEFAULT_SENDER_EXCLUDES)
        )
        href_patterns = tuple(p.lower() for p in imap_conf.get("booking_link_patterns", []))

        host = os.environ.get("DUNE_WATCH_IMAP_HOST")
        port = int(os.environ.get("DUNE_WATCH_IMAP_PORT", "993"))
        user = os.environ.get("DUNE_WATCH_IMAP_USER")
        password = os.environ.get("DUNE_WATCH_IMAP_PASS")
        if not host or not user or not password:
            raise AdapterFetchError("IMAP credentials not configured (DUNE_WATCH_IMAP_HOST/USER/PASS)")

        conn = None
        try:
            conn = imaplib.IMAP4_SSL(host, port)
            conn.login(user, password)
            status, _ = conn.select(folder)
            if status != "OK":
                raise AdapterFetchError(f"Could not select IMAP folder '{folder}'")

            since_date = (date.today() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
            uids = self._search_uids(conn, since_date, sender_filter)
            listings: list[RawListing] = []
            film_keywords = [k.lower() for k in self.app_config.film.keywords]

            for uid in uids:
                try:
                    status, msg_data = conn.fetch(uid, "(RFC822)")
                    if status != "OK" or not msg_data or msg_data[0] is None:
                        continue
                    msg = email.message_from_bytes(msg_data[0][1])
                except (imaplib.IMAP4.abort, OSError) as exc:
                    # The connection died mid-scan. Everything after this UID is
                    # unread, so returning what we have would look like a successful
                    # poll that happened to find nothing - and if the on-sale email was
                    # in the unread remainder we would never alert on it. Observed in
                    # the wild as a run of "[Errno 32] Broken pipe" warnings followed by
                    # a recorded success. Fail the poll instead so it retries and
                    # source_health notices.
                    raise AdapterFetchError(
                        f"IMAP connection lost while fetching uid={uid!r} "
                        f"({len(listings)} of {len(uids)} messages scanned): {exc}"
                    ) from exc
                except Exception as exc:
                    # A single malformed message is not a reason to abandon the scan.
                    logger.warning("Skipping unparseable IMAP message uid=%s: %s", uid, exc)
                    continue

                message_id = msg.get("Message-ID") or f"uid-{uid.decode(errors='replace')}"
                subject = _decode_header(msg.get("Subject"))
                from_addr = _decode_header(msg.get("From")).lower()

                if sender_filter and not any(s in from_addr for s in sender_filter):
                    continue
                if any(x in from_addr for x in sender_excludes):
                    logger.debug("Skipping transactional sender %r (%s)", from_addr, subject)
                    continue

                body_text, anchors = _extract_body_and_anchors(msg)
                haystack = f"{subject}\n{body_text}".lower()

                if subject_keywords and not any(k in haystack for k in subject_keywords):
                    continue
                if film_keywords and not matches_any(haystack, film_keywords):
                    continue
                if any(marker in haystack for marker in transactional_markers):
                    # A receipt for tickets you already hold, not an announcement.
                    logger.info("Skipping transactional email: %s", subject)
                    continue

                listings.extend(
                    self._build_listings(
                        message_id=message_id,
                        subject=subject,
                        body_text=body_text,
                        haystack=haystack,
                        anchors=anchors,
                        text_markers=text_markers,
                        href_patterns=href_patterns,
                    )
                )

            return listings
        except AdapterFetchError:
            raise
        except Exception as exc:
            raise AdapterFetchError(f"IMAP fetch failed: {exc}") from exc
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass

    @staticmethod
    def _search_uids(conn, since_date: str, sender_filter: list[str]) -> list[bytes]:
        """UIDs worth fetching, filtered by sender on the server.

        Without the FROM narrowing this fetches every message in the lookback window on
        every poll and applies the sender filter locally - fine at a 60-minute cadence,
        but during an on-sale the email venues poll every 60 seconds, and pulling a
        month of full RFC822 bodies that often is both slow and rude to the mail host.
        IMAP has no portable multi-value FROM, so run one search per sender and union.
        """
        if not sender_filter:
            status, data = conn.search(None, f'(SINCE "{since_date}")')
            if status != "OK":
                raise AdapterFetchError("IMAP SEARCH failed")
            return data[0].split() if data and data[0] else []

        seen: set[bytes] = set()
        ordered: list[bytes] = []
        any_ok = False
        for sender in sender_filter:
            status, data = conn.search(None, f'(SINCE "{since_date}" FROM "{sender}")')
            if status != "OK":
                logger.warning("IMAP SEARCH for sender %r failed; skipping it", sender)
                continue
            any_ok = True
            for uid in (data[0].split() if data and data[0] else []):
                if uid not in seen:
                    seen.add(uid)
                    ordered.append(uid)
        if not any_ok:
            raise AdapterFetchError("IMAP SEARCH failed for every configured sender")
        return ordered

    def _build_listings(
        self, message_id: str, subject: str, body_text: str, haystack: str,
        anchors: list[tuple[str, str]], text_markers: tuple[str, ...],
        href_patterns: tuple[str, ...],
    ) -> list[RawListing]:
        """One email can announce several showtimes (the Science Museum's 9 Sept 2026
        mail offered both 14.30 and 19.15), so emit one listing per booking anchor."""
        fingerprint = hashlib.sha256((subject + body_text).encode("utf-8")).hexdigest()
        format_label = first_match(haystack, self.app_config.film.format_keywords)

        if any(p in haystack for p in BOOKABLE_PHRASES):
            availability = "bookable"
        elif any(p in haystack for p in REGISTER_PHRASES):
            availability = "register_interest"
        else:
            availability = "unknown"

        booking_anchors = _find_booking_anchors(anchors, text_markers, href_patterns)

        def make(show_date, show_time, booking_link) -> RawListing:
            return RawListing(
                venue_id=self.venue_config.id,
                venue_name=self.venue_config.name,
                film_title=self.app_config.film.title,
                show_date=show_date,
                show_time=show_time,
                format_label=format_label,
                availability=availability,
                booking_link=booking_link,
                source_type="imap_newsletter",
                raw_fingerprint=fingerprint,
                source_message_id=message_id,
            )

        if not booking_anchors:
            return [make(_extract_date(body_text) or _extract_date(subject),
                         _extract_time(body_text), None)]

        email_date = _extract_date(body_text) or _extract_date(subject)
        email_time = _extract_time(body_text) if len(booking_anchors) == 1 else None
        out: list[RawListing] = []
        seen: set[tuple[Optional[str], Optional[str]]] = set()
        for text, href in booking_anchors:
            show_date = _extract_date(text) or email_date
            show_time = _extract_time(text) or email_time
            if (show_date, show_time) in seen:
                continue
            seen.add((show_date, show_time))
            out.append(make(show_date, show_time, href))
        return out
