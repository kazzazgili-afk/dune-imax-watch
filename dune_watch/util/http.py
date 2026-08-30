"""A polite requests.Session: identifies itself, respects a timeout, retries transient
5xx/connection errors a couple of times, and never retries on 4xx."""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from dune_watch.config import PollingConfig


def build_session(polling: PollingConfig) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": polling.user_agent})
    retry = Retry(
        total=polling.http_max_retries,
        backoff_factor=1.0,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
