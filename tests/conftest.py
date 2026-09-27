from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def load_fixture_html():
    def _load(name: str) -> str:
        return (FIXTURES_DIR / "html" / name).read_text()
    return _load


@pytest.fixture
def load_fixture_email():
    def _load(name: str) -> bytes:
        return (FIXTURES_DIR / "email" / name).read_bytes()
    return _load


@pytest.fixture
def load_fixture_json():
    import json

    def _load(name: str):
        return json.loads((FIXTURES_DIR / "json" / name).read_text())
    return _load


@pytest.fixture
def fresh_state_db(tmp_path):
    from dune_watch.engine.state_store import StateStore
    store = StateStore(tmp_path / "test_state.db")
    yield store
    store.close()


@pytest.fixture(autouse=True)
def stub_robots(monkeypatch):
    """Keep the suite hermetic.

    The HTML adapter now consults robots.txt before every fetch, which would otherwise
    make the tests hit the real network. Every test gets a permissive robots.txt with a
    20s crawl-delay (matching sciencemuseum.org.uk); tests/test_robots.py patches over
    this with its own responses.
    """
    import dune_watch.util.robots as robots_module

    class PermissiveResponse:
        status_code = 200
        text = "User-agent: *\nCrawl-Delay: 20\n"

    monkeypatch.setattr(robots_module.requests, "get", lambda *a, **k: PermissiveResponse())
    # The module-level cache is keyed by User-Agent and would otherwise leak real
    # robots.txt data between tests.
    monkeypatch.setattr(robots_module, "_SHARED", {})
