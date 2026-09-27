"""Watches a venue's public Bluesky feed for an on-sale announcement.

Why this channel exists: venues usually post "tickets are on sale now" to social at the
same moment the newsletter goes out, and sometimes earlier. Bluesky has a public,
documented, unauthenticated read API (public.api.bsky.app), so polling it needs no
account, no API key and no scraping - it is the cheapest genuinely fast signal available.

The Science Museum is on Bluesky (did:plc:vfz6n4by6ayu2ybwpq6qemik). BFI is not, as of
2026-09 - a profile lookup returns "Profile not found" - so this adapter is configured
per venue rather than assumed for all of them.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from dune_watch.adapters.base import Adapter, AdapterFetchError
from dune_watch.models import RawListing
from dune_watch.util.http import build_session
from dune_watch.util.text import first_match, matches_any, normalize_for_matching

logger = logging.getLogger("dune_watch.adapters.bluesky_feed")

PUBLIC_API = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"

ON_SALE_PHRASES = (
    "on sale", "tickets available", "book now", "buy tickets", "tickets released",
    "booking now open", "tickets are live", "now booking",
)
REGISTER_PHRASES = ("register your interest", "register interest", "sign up", "coming soon")


def _post_url(uri: str, handle: str) -> Optional[str]:
    """Turn an at:// record URI into the web permalink a human can open."""
    if not uri.startswith("at://"):
        return None
    rkey = uri.rsplit("/", 1)[-1]
    if not rkey:
        return None
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


class BlueskyFeedAdapter(Adapter):
    def fetch(self) -> list[RawListing]:
        actor = self.venue_config.extra.get("bluesky", {}).get("actor")
        if not actor:
            raise AdapterFetchError(
                f"Venue '{self.venue_config.id}' is missing bluesky.actor (handle or DID)"
            )
        limit = int(self.venue_config.extra.get("bluesky", {}).get("limit", 30))

        session = build_session(self.app_config.polling)
        try:
            response = session.get(
                PUBLIC_API,
                params={"actor": actor, "limit": limit, "filter": "posts_no_replies"},
                timeout=self.app_config.polling.http_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise AdapterFetchError(f"Bluesky feed fetch for {actor} failed: {exc}") from exc

        feed = payload.get("feed")
        if not isinstance(feed, list):
            raise AdapterFetchError(f"Unexpected Bluesky response shape for {actor}")

        film_keywords = [k.lower() for k in self.app_config.film.keywords]
        format_keywords = [k.lower() for k in self.app_config.film.format_keywords]

        listings: list[RawListing] = []
        for item in feed:
            try:
                listing = self._parse_item(item, film_keywords, format_keywords)
            except Exception as exc:
                logger.warning("Skipping malformed Bluesky post for %s: %s",
                               self.venue_config.id, exc)
                continue
            if listing is not None:
                listings.append(listing)
        return listings

    def _parse_item(
        self, item: dict, film_keywords: list[str], format_keywords: list[str]
    ) -> Optional[RawListing]:
        post = item.get("post") or {}
        record = post.get("record") or {}
        text = record.get("text") or ""
        if not text:
            return None

        haystack = normalize_for_matching(text)
        if film_keywords and not matches_any(text, film_keywords):
            return None

        # A venue posting about the film in general is not news; require a format
        # mention (IMAX/70mm) so this doesn't alert on every marketing post.
        format_label = first_match(text, format_keywords)
        if format_keywords and format_label is None:
            return None

        if any(p in haystack for p in ON_SALE_PHRASES):
            availability = "bookable"
        elif any(p in haystack for p in REGISTER_PHRASES):
            availability = "register_interest"
        else:
            availability = "unknown"

        uri = post.get("uri") or ""
        handle = ((post.get("author") or {}).get("handle")) or self.venue_config.extra.get(
            "bluesky", {}
        ).get("actor", "")

        # Prefer a link the venue put in the post itself over the post permalink.
        booking_link = self._embedded_link(post) or _post_url(uri, handle)

        return RawListing(
            venue_id=self.venue_config.id,
            venue_name=self.venue_config.name,
            film_title=self.app_config.film.title,
            show_date=None,
            show_time=None,
            format_label=format_label,
            availability=availability,
            booking_link=booking_link,
            source_type="bluesky_feed",
            raw_fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            source_message_id=uri or None,
        )

    @staticmethod
    def _embedded_link(post: dict) -> Optional[str]:
        embed = post.get("embed") or {}
        external = embed.get("external") or {}
        uri = external.get("uri")
        return uri if isinstance(uri, str) and uri.startswith("http") else None
