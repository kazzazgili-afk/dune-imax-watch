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
def fresh_state_db(tmp_path):
    from dune_watch.engine.state_store import StateStore
    store = StateStore(tmp_path / "test_state.db")
    yield store
    store.close()
