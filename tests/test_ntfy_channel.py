from __future__ import annotations

from unittest.mock import Mock

import pytest

from dune_watch.models import Alert
from dune_watch.notify.base import NotificationSendError
from dune_watch.notify.ntfy import NtfyChannel


def make_alert(urgency="CRITICAL", booking_link="https://my.sciencemuseum.org.uk/events?kid=812") -> Alert:
    return Alert(
        listing_key="science_museum_imax|pending", venue_id="science_museum_imax",
        venue_name="Science Museum IMAX", film_title="Dune: Part Three",
        show_date="2026-12-18", show_time="19:00", format_label="IMAX 70mm",
        booking_link=booking_link, event_type="on_sale_detected", urgency=urgency,
    )


@pytest.fixture
def posts(mocker, monkeypatch):
    monkeypatch.setenv("DUNE_WATCH_NTFY_TOPIC", "dune3-test")
    captured = []

    def fake_post(url, data=None, headers=None, timeout=None):
        captured.append({"url": url, "body": data.decode(), "headers": headers})
        response = Mock()
        response.raise_for_status = Mock()
        return response

    mocker.patch("dune_watch.notify.ntfy.requests.post", side_effect=fake_post)
    return captured


def channel(**settings) -> NtfyChannel:
    base = {"topic_env_var": "DUNE_WATCH_NTFY_TOPIC", "server": "https://ntfy.sh"}
    base.update(settings)
    return NtfyChannel(base)


def test_booking_link_becomes_a_tappable_action(posts):
    channel(repeat_critical_after_minutes=[]).send(make_alert())
    assert len(posts) == 1
    headers = posts[0]["headers"]
    assert headers["Click"] == "https://my.sciencemuseum.org.uk/events?kid=812"
    assert headers["Actions"].startswith("view, Open booking page, https://")
    assert headers["Priority"] == "urgent"
    assert "rotating_light" in headers["Tags"]


def test_alert_without_link_has_no_click_headers(posts):
    channel(repeat_critical_after_minutes=[]).send(make_alert(booking_link=None))
    assert "Click" not in posts[0]["headers"]
    assert "Actions" not in posts[0]["headers"]


def test_critical_schedules_server_side_followups(posts):
    """Follow-ups use ntfy's Delay header so they are delivered by the server even if
    this process or the whole machine has gone away."""
    channel(repeat_critical_after_minutes=[3, 10]).send(make_alert("CRITICAL"))
    assert len(posts) == 3
    assert "Delay" not in posts[0]["headers"]
    assert posts[1]["headers"]["Delay"] == "180s"
    assert posts[2]["headers"]["Delay"] == "600s"
    assert "reminder +3min" in posts[1]["body"]


def test_non_critical_is_sent_once(posts):
    channel(repeat_critical_after_minutes=[3, 10]).send(make_alert("HIGH"))
    assert len(posts) == 1
    assert posts[0]["headers"]["Priority"] == "high"


def test_followup_failure_does_not_mask_a_delivered_alert(mocker, monkeypatch):
    monkeypatch.setenv("DUNE_WATCH_NTFY_TOPIC", "dune3-test")
    calls = {"n": 0}

    def flaky(url, data=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            response = Mock()
            response.raise_for_status = Mock()
            return response
        raise RuntimeError("ntfy down")

    mocker.patch("dune_watch.notify.ntfy.requests.post", side_effect=flaky)
    # Must not raise: the first push landed, which is what matters.
    channel(repeat_critical_after_minutes=[3]).send(make_alert("CRITICAL"))
    assert calls["n"] == 2


def test_missing_topic_is_an_error(monkeypatch):
    monkeypatch.delenv("DUNE_WATCH_NTFY_TOPIC", raising=False)
    with pytest.raises(NotificationSendError, match="topic not configured"):
        channel().send(make_alert())
