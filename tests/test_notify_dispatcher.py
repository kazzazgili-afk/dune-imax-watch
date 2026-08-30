from __future__ import annotations

from dune_watch.models import Alert
from dune_watch.notify.base import NotificationSendError
from dune_watch.notify.browser_open import BrowserOpener
from dune_watch.notify.dispatcher import Dispatcher


def make_alert(**overrides) -> Alert:
    defaults = dict(
        listing_key="k1", venue_id="science_museum_imax", venue_name="Science Museum IMAX",
        film_title="Dune: Part Three", show_date="2026-12-19", show_time="19:30",
        format_label="IMAX 70mm", booking_link="https://example.org/book",
        event_type="new_listing", urgency="HIGH",
    )
    defaults.update(overrides)
    return Alert(**defaults)


class FakeChannel:
    def __init__(self, name, should_fail=False):
        self.name = name
        self.should_fail = should_fail
        self.sent = []

    def send(self, alert):
        if self.should_fail:
            raise NotificationSendError("simulated failure")
        self.sent.append(alert)


class FakeBrowserOpener:
    def __init__(self):
        self.opened = []

    def maybe_open(self, alert):
        self.opened.append(alert)


def test_dry_run_never_calls_real_send():
    channel = FakeChannel("ntfy")
    dispatcher = Dispatcher(channels=[channel], dry_run=True)
    dispatcher.dispatch(make_alert())
    assert channel.sent == []  # wrapped in LoggingChannel, real .send() never invoked


def test_one_channel_failure_does_not_block_others():
    good = FakeChannel("ntfy")
    bad = FakeChannel("email_smtp", should_fail=True)
    dispatcher = Dispatcher(channels=[bad, good], dry_run=False)
    result = dispatcher.dispatch(make_alert())
    assert result.channel_results["ntfy"] == "success"
    assert "failed" in result.channel_results["email_smtp"]
    assert len(good.sent) == 1


def test_unexpected_exception_in_one_channel_does_not_block_others():
    class BuggyChannel:
        name = "buggy"

        def send(self, alert):
            raise RuntimeError("bug, not a NotificationSendError")

    good = FakeChannel("ntfy")
    dispatcher = Dispatcher(channels=[BuggyChannel(), good], dry_run=False)
    result = dispatcher.dispatch(make_alert())
    assert "failed" in result.channel_results["buggy"]
    assert len(good.sent) == 1


def test_browser_opener_called_for_non_dry_run():
    opener = FakeBrowserOpener()
    dispatcher = Dispatcher(channels=[FakeChannel("ntfy")], dry_run=False, browser_opener=opener)
    dispatcher.dispatch(make_alert())
    assert len(opener.opened) == 1


def test_browser_opener_not_called_in_dry_run():
    opener = FakeBrowserOpener()
    dispatcher = Dispatcher(channels=[FakeChannel("ntfy")], dry_run=True, browser_opener=opener)
    dispatcher.dispatch(make_alert())
    assert opener.opened == []


def test_browser_opener_respects_min_urgency(mocker):
    mock_open = mocker.patch("dune_watch.notify.browser_open.webbrowser.open")
    opener = BrowserOpener(enabled=True, min_urgency="HIGH")
    opener.maybe_open(make_alert(urgency="INFO"))
    mock_open.assert_not_called()
    opener.maybe_open(make_alert(urgency="HIGH"))
    mock_open.assert_called_once_with("https://example.org/book")


def test_browser_opener_skips_when_no_link(mocker):
    mock_open = mocker.patch("dune_watch.notify.browser_open.webbrowser.open")
    opener = BrowserOpener(enabled=True, min_urgency="INFO")
    opener.maybe_open(make_alert(booking_link=None))
    mock_open.assert_not_called()


def test_browser_opener_noop_when_disabled(mocker):
    mock_open = mocker.patch("dune_watch.notify.browser_open.webbrowser.open")
    opener = BrowserOpener(enabled=False, min_urgency="INFO")
    opener.maybe_open(make_alert(urgency="CRITICAL"))
    mock_open.assert_not_called()
