"""SQLite-backed dedup state: what listings we've seen, their current availability,
poll history, and per-source failure streaks. Atomic writes matter here since a
launchd/systemd cycle can be killed mid-run."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Optional

from dune_watch.models import RawListing, StoredListing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    listing_key TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL,
    venue_name TEXT NOT NULL,
    film_title TEXT NOT NULL,
    show_date TEXT,
    show_time TEXT,
    format_label TEXT,
    availability TEXT NOT NULL,
    booking_link TEXT,
    source_type TEXT NOT NULL,
    raw_fingerprint TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_alerted_at TEXT,
    last_alert_urgency TEXT
);

CREATE TABLE IF NOT EXISTS poll_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    success INTEGER NOT NULL,
    error_message TEXT,
    listings_found INTEGER
);

CREATE TABLE IF NOT EXISTS source_health (
    venue_id TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure_at TEXT,
    last_success_at TEXT,
    failure_alert_sent INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alerts_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    urgency TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    channels_success TEXT,
    channels_failed TEXT
);
"""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class StateStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path not in (":memory:",):
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def get_listing(self, listing_key: str) -> Optional[StoredListing]:
        row = self.conn.execute("SELECT * FROM listings WHERE listing_key = ?", (listing_key,)).fetchone()
        if row is None:
            return None
        return StoredListing(**{k: row[k] for k in row.keys()})

    def upsert_listing(self, listing: RawListing) -> None:
        key = listing.listing_key()
        now = now_iso()
        existing = self.get_listing(key)
        first_seen = existing.first_seen_at if existing else now
        self.conn.execute(
            """
            INSERT INTO listings (
                listing_key, venue_id, venue_name, film_title, show_date, show_time,
                format_label, availability, booking_link, source_type, raw_fingerprint,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_key) DO UPDATE SET
                availability=excluded.availability,
                booking_link=excluded.booking_link,
                raw_fingerprint=excluded.raw_fingerprint,
                show_date=excluded.show_date,
                show_time=excluded.show_time,
                format_label=excluded.format_label,
                last_seen_at=excluded.last_seen_at
            """,
            (
                key, listing.venue_id, listing.venue_name, listing.film_title,
                listing.show_date, listing.show_time, listing.format_label,
                listing.availability, listing.booking_link, listing.source_type,
                listing.raw_fingerprint, first_seen, now,
            ),
        )
        self.conn.commit()

    def record_alert_sent(
        self, listing_key: str, event_type: str, urgency: str,
        channels_success: list[str], channels_failed: list[str],
    ) -> None:
        now = now_iso()
        self.conn.execute(
            "UPDATE listings SET last_alerted_at = ?, last_alert_urgency = ? WHERE listing_key = ?",
            (now, urgency, listing_key),
        )
        self.conn.execute(
            """INSERT INTO alerts_sent (listing_key, event_type, urgency, sent_at, channels_success, channels_failed)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (listing_key, event_type, urgency, now, ",".join(channels_success), ",".join(channels_failed)),
        )
        self.conn.commit()

    def record_poll_start(self, venue_id: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO poll_log (venue_id, started_at, success, listings_found) VALUES (?, ?, 0, 0)",
            (venue_id, now_iso()),
        )
        self.conn.commit()
        return cur.lastrowid

    def record_poll_finish(self, poll_id: int, success: bool, error_message: Optional[str], listings_found: int) -> None:
        self.conn.execute(
            "UPDATE poll_log SET finished_at = ?, success = ?, error_message = ?, listings_found = ? WHERE id = ?",
            (now_iso(), int(success), error_message, listings_found, poll_id),
        )
        self.conn.commit()

    def record_source_success(self, venue_id: str) -> None:
        now = now_iso()
        self.conn.execute(
            """INSERT INTO source_health (venue_id, consecutive_failures, last_success_at, failure_alert_sent)
               VALUES (?, 0, ?, 0)
               ON CONFLICT(venue_id) DO UPDATE SET
                   consecutive_failures = 0, last_success_at = excluded.last_success_at, failure_alert_sent = 0""",
            (venue_id, now),
        )
        self.conn.commit()

    def record_source_failure(self, venue_id: str) -> int:
        now = now_iso()
        self.conn.execute(
            """INSERT INTO source_health (venue_id, consecutive_failures, last_failure_at)
               VALUES (?, 1, ?)
               ON CONFLICT(venue_id) DO UPDATE SET
                   consecutive_failures = consecutive_failures + 1, last_failure_at = excluded.last_failure_at""",
            (venue_id, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT consecutive_failures FROM source_health WHERE venue_id = ?", (venue_id,)
        ).fetchone()
        return row["consecutive_failures"] if row else 0

    def mark_failure_alert_sent(self, venue_id: str) -> None:
        self.conn.execute("UPDATE source_health SET failure_alert_sent = 1 WHERE venue_id = ?", (venue_id,))
        self.conn.commit()

    def get_source_health(self, venue_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM source_health WHERE venue_id = ?", (venue_id,)).fetchone()

    def all_listings(self) -> list[StoredListing]:
        rows = self.conn.execute("SELECT * FROM listings ORDER BY venue_id, show_date, show_time").fetchall()
        return [StoredListing(**{k: row[k] for k in row.keys()}) for row in rows]
