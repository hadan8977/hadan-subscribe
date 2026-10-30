# Adapted from quantcheck offline regression tests (MIT); no legacy runtime imports.
import copy
import unittest

from hadan.compat.diff import (
    DYNAMIC_NOISE_FIELDS,
    compare,
    diff_rows,
    parse_analyst_signal,
)


def sample_picks():
    return {
        "monthly": {
            "pick_date": "Unknown",
            "rows": [
                {
                    "symbol": "ABC",
                    "company": "Alpha Corp",
                    "return": "+1.1%",
                    "rating": "Buy",
                    "gt_score": "91",
                    "current_price": "$10.00",
                    "buy_or_entry_price": "$9.50",
                }
            ],
        },
        "weekly": {
            "pick_date": "05/11/26",
            "rows": [
                {
                    "symbol": "XYZ",
                    "company": "Xylon Inc",
                    "sector": "Tech",
                    "rating": "Strong Buy",
                    "gt_score": "88",
                    "current_price": "$20.00",
                    "buy_or_entry_price": "$18.00",
                    "analyst_signal": "Buy +0.26",
                }
            ],
        },
    }


def weekly_change(old, new, field="analyst_signal"):
    """compare() result for one weekly row whose `field` goes from old to new."""
    before = sample_picks()
    after = copy.deepcopy(before)
    before["weekly"]["rows"][0][field] = old
    after["weekly"]["rows"][0][field] = new
    return compare(before, after)


class DiffTests(unittest.TestCase):
    def test_diff_rows_reports_static_changes_and_ignores_noise_and_scraper_gaps(self):
        base = {"symbol": "ABC", "company": "Alpha", "rating": "Buy"}
        self.assertEqual(
            diff_rows([base], [{**base, "rating": "Hold"}])["changed"],
            [{"symbol": "ABC", "fields": {"rating": {"old": "Buy", "new": "Hold"}}}],
        )
        self.assertEqual(
            diff_rows(
                [{**base, "current_price": "$10.00"}],
                [{**base, "current_price": "$11.00"}],
            ),
            {"added": [], "removed": [], "changed": []},
        )
        # A newly-missing rating is a scraper gap, not a pick change.
        self.assertEqual(
            diff_rows([{**base, "rating": "Strong Buy"}], [{**base, "rating": None}])[
                "changed"
            ],
            [],
        )
        self.assertEqual(
            diff_rows([base], [base, {"symbol": "NEW", "company": "New Co"}])["added"],
            ["NEW"],
        )
        self.assertEqual(diff_rows([base], [])["removed"], ["ABC"])

    def test_every_dynamic_noise_field_is_ignored(self):
        for field in (
            "source_kind",
            "signal_price",
            "signal_date",
            "signal_at",
            "watch_reason",
            "analyst_signal_unavailable",
            "next_earnings_unavailable",
            "gt_score_source",
            "current_price",
            "return",
        ):
            self.assertIn(field, DYNAMIC_NOISE_FIELDS)
        for field in sorted(DYNAMIC_NOISE_FIELDS):
            with self.subTest(field):
                self.assertFalse(weekly_change("", "now watchlist", field)["changed"])
                self.assertFalse(weekly_change(True, False, field)["changed"])

    def test_noise_does_not_trigger_notification(self):
        old = sample_picks()
        new = copy.deepcopy(old)
        old["fetched_at"] = "2026-05-21T08:30:00-04:00"
        new["fetched_at"] = "2026-05-21T17:00:00-04:00"
        new["monthly"]["rows"][0]["current_price"] = "$10.42"
        new["weekly"]["rows"][0]["current_price"] = "$20.55"
        self.assertFalse(compare(old, new)["changed"])

        # Unknown dates are not source changes.
        old = {
            "monthly": {"pick_date": "May 2026", "rows": []},
            "weekly": {"pick_date": "Unknown", "rows": []},
        }
        new = {
            "monthly": {"pick_date": "Unknown", "rows": []},
            "weekly": {"pick_date": "05/22/26", "rows": []},
        }
        self.assertFalse(compare(old, new)["changed"])

    def test_real_date_change_is_reported(self):
        old, new = sample_picks(), sample_picks()
        new["weekly"]["pick_date"] = "05/18/26"
        result = compare(old, new)
        self.assertTrue(result["changed"])
        self.assertEqual(
            result["weekly"]["date"], {"old": "05/11/26", "new": "05/18/26"}
        )
        self.assertFalse(result["monthly"]["changed_flag"])

    def test_analyst_signal_parsing_and_threshold(self):
        self.assertEqual(parse_analyst_signal("Strong Buy +0.27"), ("Strong Buy", 0.27))
        self.assertEqual(parse_analyst_signal(""), ("", None))
        self.assertEqual(parse_analyst_signal("Neutral"), ("Neutral", None))

        # Daily oscillation seen in production (2026-09-22/23) and small score noise: no email.
        for old, new in [
            ("Buy +0.38", "Strong Buy +0.56"),
            ("Strong Buy +0.56", "Buy +0.31"),
            ("Sell -0.16", "Buy +0.38"),
            ("Buy +0.26", "Buy +0.29"),
        ]:
            with self.subTest(old=old, new=new):
                self.assertFalse(weekly_change(old, new)["changed"])
        # Large swings and Strong Sell entry/exit still notify.
        for old, new in [
            ("Sell -0.22", "Strong Buy +0.51"),
            ("Sell -0.29", "Strong Sell -0.52"),
            ("Strong Sell -0.52", "Sell -0.40"),
        ]:
            with self.subTest(old=old, new=new):
                self.assertTrue(weekly_change(old, new)["changed"])


if __name__ == "__main__":
    unittest.main()
