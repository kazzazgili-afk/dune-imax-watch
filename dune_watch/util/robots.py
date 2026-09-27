"""robots.txt as an enforced runtime constraint rather than a comment in config.

Two jobs:

1. Refuse to fetch a path the site disallows. Politeness that lives only in a YAML
   comment stops being true the moment someone edits the YAML.
2. Clamp each venue's poll interval to the site's own `Crawl-delay`. This is what
   makes fast polling defensible: sciencemuseum.org.uk publishes `Crawl-Delay: 20`,
   so a 20-second poll is explicitly within what the site asks for, while the
   configured 20 *minutes* was 60x more conservative than required.

Fail-safe direction matters: if robots.txt cannot be read we allow the fetch but keep
the configured interval, because an unreachable robots.txt is not permission to hammer.

robots.txt is fetched with this project's own identifying User-Agent, not urllib's
default. `RobotFileParser.read()` uses `Python-urllib/x.y`, which both sciencemuseum.org.uk
and whatson.bfi.org.uk answer with 403 - and the parser treats a 403 as "disallow
everything", so every venue looked disallowed while Vue (whose robots.txt genuinely
disallows the booking paths) looked allowed. Fetching it ourselves fixes both.
"""
from __future__ import annotations

import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse

import requests

logger = logging.getLogger("dune_watch.util.robots")

# robots.txt rarely changes; re-reading it on every poll would itself be impolite.
CACHE_TTL_SECONDS = 3600


@dataclass
class RobotsVerdict:
    allowed: bool
    crawl_delay_seconds: Optional[float]
    rule_source: str

    def describe(self) -> str:
        delay = (
            f"crawl-delay {self.crawl_delay_seconds:g}s"
            if self.crawl_delay_seconds is not None
            else "no crawl-delay"
        )
        return f"{'allowed' if self.allowed else 'DISALLOWED'}, {delay} ({self.rule_source})"


class RobotsCache:
    def __init__(self, user_agent: str, ttl_seconds: int = CACHE_TTL_SECONDS):
        self.user_agent = user_agent
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, Optional[urllib.robotparser.RobotFileParser]]] = {}

    def _robots_url(self, url: str) -> tuple[str, str]:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        return origin, urlunparse((parts.scheme, parts.netloc, "/robots.txt", "", "", ""))

    def _parser(self, url: str) -> Optional[urllib.robotparser.RobotFileParser]:
        origin, robots_url = self._robots_url(url)
        cached = self._cache.get(origin)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self.ttl_seconds:
            return cached[1]

        parser: Optional[urllib.robotparser.RobotFileParser] = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            response = requests.get(
                robots_url, headers={"User-Agent": self.user_agent}, timeout=10
            )
        except requests.RequestException as exc:
            logger.warning("Could not read %s (%s); allowing fetch, keeping configured interval",
                           robots_url, exc)
            parser = None
        else:
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            elif response.status_code == 404:
                # No robots.txt at all means nothing is disallowed.
                parser.allow_all = True
            elif response.status_code in (401, 403):
                # Convention treats a protected robots.txt as "stay out"; surface it
                # loudly rather than silently polling a site that won't show its rules.
                logger.warning("%s returned %s - treating the whole host as disallowed",
                               robots_url, response.status_code)
                parser.disallow_all = True
            else:
                logger.warning("%s returned %s; allowing fetch, keeping configured interval",
                               robots_url, response.status_code)
                parser = None

        self._cache[origin] = (now, parser)
        return parser

    def check(self, url: str) -> RobotsVerdict:
        parser = self._parser(url)
        if parser is None:
            return RobotsVerdict(True, None, "robots.txt unavailable")

        allowed = parser.can_fetch(self.user_agent, url)
        # RobotFileParser.crawl_delay() only consults the matching agent block, and
        # returns None when the directive is absent.
        delay = parser.crawl_delay(self.user_agent)
        if delay is None:
            delay = parser.crawl_delay("*")
        try:
            delay_seconds = float(delay) if delay is not None else None
        except (TypeError, ValueError):
            delay_seconds = None
        return RobotsVerdict(allowed, delay_seconds, "robots.txt")

    def effective_interval_seconds(self, url: str, configured_seconds: float) -> float:
        """The site's crawl-delay is a floor, never a ceiling."""
        verdict = self.check(url)
        if verdict.crawl_delay_seconds is None:
            return configured_seconds
        return max(configured_seconds, verdict.crawl_delay_seconds)


_SHARED: dict[str, RobotsCache] = {}


def get_cache(user_agent: str) -> RobotsCache:
    """One cache per User-Agent for the life of the process, so a `run --loop` that
    polls every 20s re-reads robots.txt hourly rather than on every cycle."""
    cache = _SHARED.get(user_agent)
    if cache is None:
        cache = RobotsCache(user_agent)
        _SHARED[user_agent] = cache
    return cache
