from __future__ import annotations

from abc import ABC, abstractmethod

from dune_watch.config import AppConfig, VenueConfig
from dune_watch.models import RawListing


class AdapterFetchError(Exception):
    """Raised by adapters on any recoverable failure (network, parse, IMAP auth).

    The poller catches this, logs it, updates source_health, and moves on to the
    next venue rather than letting one broken source take down the whole cycle.
    """


class Adapter(ABC):
    """One instance per venue per poll cycle."""

    def __init__(self, venue_config: VenueConfig, app_config: AppConfig):
        self.venue_config = venue_config
        self.app_config = app_config

    @abstractmethod
    def fetch(self) -> list[RawListing]:
        ...
