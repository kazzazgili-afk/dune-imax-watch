from __future__ import annotations

from dune_watch.adapters.base import Adapter
from dune_watch.adapters.html_page_diff import HtmlPageDiffAdapter
from dune_watch.adapters.imap_newsletter import ImapNewsletterAdapter
from dune_watch.config import AppConfig, VenueConfig

ADAPTER_REGISTRY: dict[str, type[Adapter]] = {
    "html_page_diff": HtmlPageDiffAdapter,
    "imap_newsletter": ImapNewsletterAdapter,
    # future: "json_api": JsonApiAdapter, "rss": RssAdapter
}


def build_adapter(venue_config: VenueConfig, app_config: AppConfig) -> Adapter:
    cls = ADAPTER_REGISTRY.get(venue_config.venue_type)
    if cls is None:
        raise ValueError(f"Unknown venue_type '{venue_config.venue_type}' for venue '{venue_config.id}'")
    return cls(venue_config, app_config)
