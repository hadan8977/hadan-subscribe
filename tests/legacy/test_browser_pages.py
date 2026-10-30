# Adapted from quantcheck offline regression tests (MIT); no legacy runtime imports.
"""Browser-facing helpers exercised with fake Playwright pages (no browser is ever launched)."""

import types
import unittest

from hadan.sources import quantgt_browser as picks_report


class FakeLocator:
    def __init__(self, count=0):
        self._count = count
        self.clicked = False

    def count(self):
        return self._count

    def click(self, timeout=None):
        self.clicked = True


class FakeDialog:
    def __init__(self, visible=True, close_count=1):
        self.visible = visible
        self.close = FakeLocator(close_count)

    def count(self):
        return 1 if self.visible else 0

    def get_by_role(self, role, name=None):
        return self.close

    def locator(self, selector):
        return self.close


class FakeOverlayPage:
    def __init__(self, dialog):
        self.dialog = dialog
        self.waited = False

    def get_by_role(self, role, name=None):
        return self.dialog

    def wait_for_timeout(self, milliseconds):
        self.waited = True


class FakeCookieContext:
    def cookies(self, base):
        return [{"name": "__Secure-authjs.session-token"}]


class FakeSubscriptionGatePage:
    """Auth cookie present and pick text visible, but the page still says subscribe."""

    context = FakeCookieContext()

    def locator(self, selector):
        return FakeLocator()

    def get_by_role(self, role, name=None):
        return FakeLocator()

    def evaluate(self, script):
        return True


class ExpandedPicksDetailsTests(unittest.TestCase):
    def test_chart_layout_preserves_table_price_and_parses_consensus(self):
        row = {"symbol": "MRNA", "current_price": "$202.63", "gt_score": "4.61/5"}
        page = types.SimpleNamespace(
            evaluate=lambda script, symbol: {
                "current_price": "",
                "buy_or_entry_price": "",
                "next_earnings": "Nov 6, 2026",
                "analyst_signal": "Neutral -0.02 Moderna, Inc. provides messenger RNA medicines. Headlines",
            }
        )

        result = picks_report.expand_and_attach_details(page, [row], "monthly")

        self.assertEqual(result[0]["current_price"], "$202.63")
        self.assertEqual(result[0]["gt_score"], "4.61/5")
        self.assertEqual(result[0]["next_earnings"], "Nov 6, 2026")
        self.assertEqual(result[0]["analyst_signal"], "Neutral -0.02")

    def test_missing_details_remain_an_error_and_do_not_fabricate_metrics(self):
        row = {"symbol": "MRNA", "current_price": "$202.63"}
        page = types.SimpleNamespace(
            evaluate=lambda script, symbol: {
                "detail_error": "detail metrics did not load"
            }
        )

        result = picks_report.expand_and_attach_details(page, [row], "monthly")

        self.assertEqual(result[0]["detail_error"], "detail metrics did not load")
        self.assertEqual(result[0]["current_price"], "$202.63")
        self.assertEqual(result[0]["analyst_signal"], "")
        self.assertNotIn("next_earnings", result[0])


class PageValidationTests(unittest.TestCase):
    def test_subscription_gate_javascript_keeps_regex_word_boundaries(self):
        scripts = []
        page = types.SimpleNamespace(
            evaluate=lambda script: scripts.append(script) or False
        )

        picks_report.has_subscription_gate(page)

        self.assertTrue(scripts)
        self.assertNotIn(
            "\x08",
            scripts[0],
            "Python string escaping must not turn JavaScript word boundaries into backspaces",
        )
        self.assertIn(r"\b(subscribe", scripts[0])

    def test_has_auth_session_checks_the_session_cookie_and_never_raises(self):
        class NoCookies:
            context = types.SimpleNamespace(cookies=lambda base: [{"name": "other"}])

        class Broken:
            @property
            def context(self):
                raise RuntimeError("page closed")

        self.assertTrue(picks_report.has_auth_session(FakeSubscriptionGatePage()))
        self.assertFalse(picks_report.has_auth_session(NoCookies()))
        self.assertFalse(picks_report.has_auth_session(Broken()))

    def test_subscription_gate_is_rejected_even_with_auth_cookie_and_pick_text(self):
        with self.assertRaisesRegex(RuntimeError, "subscription/paywall"):
            picks_report.assert_authenticated_page(FakeSubscriptionGatePage(), "weekly")

    def test_watchlist_dialog_paywall_detection(self):
        self.assertTrue(
            picks_report.is_watchlist_dialog_paywalled(
                "SNDK Subscriber-only pick Subscribe to unlock the full watchlist"
            )
        )
        self.assertFalse(
            picks_report.is_watchlist_dialog_paywalled(
                "SNDK Sandisk Corporation Momentum 1.96/2 Relative Strength 3.00/3"
            )
        )


if __name__ == "__main__":
    unittest.main()
