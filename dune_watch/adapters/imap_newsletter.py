"""Reads the user's own mailbox (read-only IMAP) for the official BFI IMAX on-sale
newsletter the user signed up for at bfi.org.uk/bfi-imax. This never touches BFI's
Cloudflare-protected ticketing site - it only reads mail the user already owns."""
from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import os
import re
from datetime import date, timedelta
from email.message import Message
from typing import Optional

from bs4 import BeautifulSoup

from dune_watch.adapters.base import Adapter, AdapterFetchError
from dune_watch.models import RawListing

logger = logging.getLogger("dune_watch.adapters.imap_newsletter")

BOOKABLE_PHRASES = ("on sale", "tickets available", "book now", "buy tickets")
REGISTER_PHRASES = ("coming soon", "sign up", "register your interest", "register interest")

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


def _extract_text_and_links(msg: Message) -> tuple[str, list[str]]:
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
    links: list[str] = []
    for html in html_parts:
        soup = BeautifulSoup(html, "html.parser")
        if not body_text:
            body_text += " " + soup.get_text(separator=" ", strip=True)
        for a in soup.find_all("a", href=True):
            links.append(a["href"])

    links.extend(_URL_PATTERN.findall(body_text))
    return body_text.strip(), links


class ImapNewsletterAdapter(Adapter):
    def fetch(self) -> list[RawListing]:
        imap_conf = self.venue_config.extra.get("imap", {})
        folder = imap_conf.get("folder", "INBOX")
        sender_filter = [s.lower() for s in imap_conf.get("sender_filter", [])]
        subject_keywords = [k.lower() for k in imap_conf.get("subject_keywords", [])]
        lookback_days = int(imap_conf.get("lookback_days_on_first_run", 30))

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
            status, data = conn.search(None, f'(SINCE "{since_date}")')
            if status != "OK":
                raise AdapterFetchError("IMAP SEARCH failed")

            uids = data[0].split() if data and data[0] else []
            listings: list[RawListing] = []
            film_keywords = [k.lower() for k in self.app_config.film.keywords]

            for uid in uids:
                try:
                    status, msg_data = conn.fetch(uid, "(RFC822)")
                    if status != "OK" or not msg_data or msg_data[0] is None:
                        continue
                    msg = email.message_from_bytes(msg_data[0][1])
                except Exception as exc:
                    logger.warning("Failed to parse IMAP message uid=%s: %s", uid, exc)
                    continue

                message_id = msg.get("Message-ID") or f"uid-{uid.decode(errors='replace')}"
                subject = msg.get("Subject", "")
                from_addr = (msg.get("From") or "").lower()

                if sender_filter and not any(s in from_addr for s in sender_filter):
                    continue

                body_text, links = _extract_text_and_links(msg)
                haystack = f"{subject}\n{body_text}".lower()

                if subject_keywords and not any(k in haystack for k in subject_keywords):
                    continue
                if film_keywords and not any(k in haystack for k in film_keywords):
                    continue

                booking_link = next((link for link in links if "bfi.org.uk" in link.lower()), None)
                show_date = _extract_date(body_text) or _extract_date(subject)
                show_time = _extract_time(body_text)

                if any(p in haystack for p in BOOKABLE_PHRASES):
                    availability = "bookable"
                elif any(p in haystack for p in REGISTER_PHRASES):
                    availability = "register_interest"
                else:
                    availability = "unknown"

                format_label = next(
                    (kw for kw in self.app_config.film.format_keywords if kw.lower() in haystack), None
                )

                fingerprint = hashlib.sha256((subject + body_text).encode("utf-8")).hexdigest()

                listings.append(RawListing(
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
                ))

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
