"""Offline regressions for Watchlist dialog/page boundary porting (2026-10-27).

All company names, dates and figures below are invented fixtures, not captured
subscriber data. Fake locators exercise actual Python parsing; Node, when present,
executes the actual emitted JavaScript with fixture document/location values.
No credentials, real site, network calls, or production files are used.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from hadan.compat.validation import validate_member_picks_data
from hadan.sources import quantgt_browser as browser


SYMBOLS = ("MRNA", "TEAM", "VEEV", "P", "NTRA", "AXA", "AXB", "AXC", "AXD", "AXE")


def card(symbol):
    return {"symbol": symbol, "company": "Fixture Example Company", "sector": "Fixture Sector",
            "current_price": "$100.00", "gt_score": "4.50/5", "gt_score_source": "weekly_api_score"}


def dialog_text(symbol, *, unavailable=False):
    metrics = "Next Earnings — Analyst Consensus —" if unavailable else "Next Earnings Nov 12, 2026 Analyst Consensus Neutral -0.09"
    return (f"{symbol} Fixture Sector Fixture Example Company PRICE $100.00 "
            f"{symbol} : $98.00 P/E (TTM) 20.10 Market Cap $12.00B Revenue (TTM) $3.00B "
            f"Revenue Growth (YoY) +4.00% {metrics} "
            "Fixture-only company description. More Headlines Close")


class FakeClick:
    def __init__(self, action):
        self.action = action

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def click(self):
        self.action()


class FakeMain:
    def __init__(self, page):
        self.page = page

    def get_by_text(self, text, exact=False):
        assert exact is True
        assert text in self.page.dialogs
        return FakeClick(lambda: self.page.open(text))


class FakeDialog:
    def __init__(self, page):
        self.page = page

    def wait_for(self, state, timeout):
        assert bool(self.page.current) == (state == "visible")

    def inner_text(self, timeout):
        return self.page.dialogs[self.page.current]


class FakeWatchlistPage:
    def __init__(self, dialogs):
        self.dialogs = dialogs
        self.current = None
        self.opened = []
        self.closed = []
        self.keyboard = SimpleNamespace(press=lambda key: self.close(key))

    def open(self, symbol):
        self.current = symbol
        self.opened.append(symbol)

    def close(self, method):
        self.closed.append(method)
        self.current = None

    def locator(self, selector):
        if selector == "main":
            return FakeMain(self)
        assert selector == '[role="dialog"]'
        return FakeDialog(self)

    def get_by_role(self, role, name=None):
        assert (role, name) == ("button", "Close")
        return FakeClick(lambda: self.close("Close"))

    def get_by_text(self, *_args, **_kwargs):
        raise AssertionError("Do not click page-wide ticker text: a short ticker can match an account avatar")


@pytest.mark.parametrize("symbol", ["MRNA", "TEAM", "VEEV", "P", "NTRA", "BRK.B"])
def test_normal_dialog_is_opened_once_and_its_real_fields_are_attached(symbol):
    page = FakeWatchlistPage({symbol: dialog_text(symbol)})
    rows = browser.expand_watchlist_and_attach_details(page, [card(symbol)])
    assert rows[0]["next_earnings"] == "Nov 12, 2026"
    assert rows[0]["analyst_signal"] == "Neutral -0.09"
    assert rows[0]["buy_or_entry_price"] == "$98.00"
    assert rows[0]["gt_score"] == "4.50/5"  # Native API score is retained.
    assert rows[0]["next_earnings_unavailable"] is False
    assert rows[0]["analyst_signal_unavailable"] is False
    assert "detail_error" not in rows[0]
    assert page.opened == [symbol] and page.closed == ["Close"]


@pytest.mark.parametrize("expected,wrong", [("MRNA", "MRNAX"), ("P", "PX"), ("BRK.B", "BRKXB")])
def test_other_ticker_dialog_is_rejected_without_attaching_its_metrics(expected, wrong):
    page = FakeWatchlistPage({expected: dialog_text(wrong)})
    row = browser.expand_watchlist_and_attach_details(page, [card(expected)])[0]
    assert "dialog symbol mismatch" in row["detail_error"]
    assert "next_earnings" not in row and "analyst_signal" not in row
    assert page.opened == [expected, expected]
    assert page.closed == ["Escape", "Escape"]


def capture(rows):
    monthly = {**card("MONTHLY"), "return": "+1.00%", "next_earnings": "Nov 12, 2026", "analyst_signal": "Neutral -0.09"}
    return {"monthly": {"pick_date": "Oct 2026", "rows": [monthly]},
            "weekly": {"kind": "watchlist", "pick_date": "Updated on Oct 23, 2026", "rows": rows}}


def test_complete_fixture_passes_unchanged_gate_but_missing_labels_still_fail():
    dialogs = {symbol: dialog_text(symbol) for symbol in SYMBOLS}
    page = FakeWatchlistPage(dialogs)
    complete = browser.expand_watchlist_and_attach_details(page, [card(symbol) for symbol in SYMBOLS])
    validate_member_picks_data(capture(complete))

    incomplete_dialogs = {**dialogs, "MRNA": "MRNA Fixture Company PRICE $100.00 Chart loading Close"}
    incomplete = browser.expand_watchlist_and_attach_details(FakeWatchlistPage(incomplete_dialogs), [card(symbol) for symbol in SYMBOLS])
    with pytest.raises(RuntimeError, match="MRNA missing next_earnings,analyst_signal"):
        validate_member_picks_data(capture(incomplete))


def test_explicit_source_unavailable_flags_remain_distinct_from_failed_loading():
    dialogs = {symbol: dialog_text(symbol, unavailable=symbol == "P") for symbol in SYMBOLS}
    rows = browser.expand_watchlist_and_attach_details(FakeWatchlistPage(dialogs), [card(symbol) for symbol in SYMBOLS])
    validate_member_picks_data(capture(rows))
    unavailable = next(row for row in rows if row["symbol"] == "P")
    assert unavailable["next_earnings"] == "" and unavailable["next_earnings_unavailable"] is True
    assert unavailable["analyst_signal"] == "" and unavailable["analyst_signal_unavailable"] is True
    fabricated = copy.deepcopy(rows)
    next(row for row in fabricated if row["symbol"] == "P").pop("next_earnings_unavailable")
    with pytest.raises(RuntimeError, match="P missing next_earnings"):
        validate_member_picks_data(capture(fabricated))


def test_paywall_dialog_remains_a_hard_error():
    page = FakeWatchlistPage({"MRNA": "MRNA Subscriber-only pick Subscribe to unlock the full watchlist"})
    with pytest.raises(RuntimeError, match="paywalled"):
        browser.expand_watchlist_and_attach_details(page, [card("MRNA")])


def test_watchlist_javascript_identifies_real_word_boundaries_offline():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional JavaScript semantic regression needs an existing Node executable; Python dialog regressions still run")
    scripts = []
    browser.is_watchlist_page(SimpleNamespace(evaluate=lambda script: scripts.append(script) or False))
    assert len(scripts) == 1
    cases = [
        {"path": "/weekly-picks", "title": "Fixture", "main": "GT Score", "body": "", "expected": True},
        {"path": "/other", "title": "Watchlist | Fixture", "main": "GT Score", "body": "", "expected": True},
        {"path": "/other", "title": "Fixture", "main": "Updated\nWatchlist\nfor this week", "body": "", "expected": True},
        {"path": "/other", "title": "Fixture", "main": None, "body": "Current Watchlist", "expected": True},
        {"path": "/weekly-picksx", "title": "Fixture", "main": "GT Score", "body": "", "expected": False},
        {"path": "/other", "title": "Watchlisted names", "main": "MyWatchlist", "body": "", "expected": False},
        {"path": "/quantgt-picks", "title": "Portfolio", "main": r"\bWatchlist\b", "body": "", "expected": False},
    ]
    program = "const check = (" + scripts[0] + ");\n" + r"""
      const cases = JSON.parse(process.argv[1]);
      const results = cases.map(item => {
        global.location = {pathname: item.path};
        global.document = {title:item.title, body:{innerText:item.body},
          querySelector: () => item.main === null ? null : {innerText:item.main}};
        return check();
      });
      process.stdout.write(JSON.stringify(results));
    """
    completed = subprocess.run([node, "-e", program, json.dumps(cases)], check=True, text=True, capture_output=True, timeout=10)
    assert json.loads(completed.stdout) == [case["expected"] for case in cases]


class _ScreenshotPage:
    def __init__(self, rows):
        self.rows = rows

    def evaluate(self, _script, _mode):
        header = ["Company", "Symbol", "Price", "Sector", "GT Score"]
        return {"matrix": [header, *self.rows], "cards": []}


def _screenshot_row(symbol="MRNA", score="4.50/5", company="Fixture Example Company"):
    return [company, symbol, "$100.00", "Fixture Sector", score]


def test_screenshot_stable_fields_must_match_the_captured_edition():
    expected = [
        {
            "symbol": "MRNA",
            "company": "Fixture Example Company",
            "sector": "Fixture Sector",
            "gt_score": "4.50/5",
        }
    ]
    browser._assert_screenshot_symbols_match(
        _ScreenshotPage([_screenshot_row()]), "weekly", expected
    )
    with pytest.raises(RuntimeError, match="changed gt_score"):
        browser._assert_screenshot_symbols_match(
            _ScreenshotPage([_screenshot_row(score="4.99/5")]), "weekly", expected
        )
    with pytest.raises(RuntimeError, match="changed company"):
        browser._assert_screenshot_symbols_match(
            _ScreenshotPage([_screenshot_row(company="Revised Edition Inc.")]),
            "weekly",
            expected,
        )
