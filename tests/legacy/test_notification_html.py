# Adapted from quantcheck offline regression tests (MIT); no legacy runtime imports.
import unittest

from hadan.compat.picks_email import build_html as build_notification_html


class NotificationHtmlTests(unittest.TestCase):
    def test_changes_are_grouped_by_list_with_added_removed_and_field_lines(self):
        data = {
            "fetched_at": "2026-05-24T16:10:09",
            "source": "https://quantgt.io",
            "monthly": {"pick_date": "Unknown", "rows": []},
            "weekly": {"pick_date": "Week of May 25, 2026", "rows": []},
        }
        diff = {
            "changed": True,
            "monthly": {
                "changed_flag": True,
                "date": None,
                "added": [],
                "removed": [],
                "changed": [
                    {
                        "symbol": "AAOI",
                        "fields": {
                            "analyst_signal": {
                                "old": "Neutral +0.02",
                                "new": "Buy +0.45",
                            },
                            "held_since": {"old": "04/2026", "new": "2026-04-01"},
                        },
                    },
                ],
            },
            "weekly": {
                "changed_flag": True,
                "date": {"old": "05/11/26", "new": "Week of May 25, 2026"},
                "added": ["INTC", "STX"],
                "removed": ["GLW", "PL"],
                "changed": [
                    {
                        "symbol": "DOCN",
                        "fields": {
                            "analyst_signal": {
                                "old": "Strong Buy +0.60",
                                "new": "Buy +0.38",
                            },
                            "gt_score": {"old": "4.41/5", "new": "4.53/5"},
                        },
                    },
                ],
            },
        }

        html = build_notification_html(data, diff)

        self.assertIn("What changed", html)
        self.assertIn("Portfolio", html)
        self.assertIn("Weekly Picks", html)
        self.assertIn("ADDED", html)
        self.assertIn("REMOVED", html)
        self.assertIn("INTC", html)
        self.assertIn("GLW", html)
        self.assertIn("AAOI", html)
        self.assertIn("DOCN", html)
        self.assertIn("Neutral +0.02", html)
        self.assertIn("Buy +0.45", html)
        self.assertIn("Strong Buy +0.60", html)
        self.assertIn("Buy +0.38", html)
        self.assertIn("(-0.22)", html)  # signal delta, coloured
        self.assertIn("GT Score re-rated", html)  # gt_score changes collapse into chips
        self.assertNotIn("<ul", html)
        # internal run metadata is never shown to subscribers
        self.assertNotIn("window=forced", html)
        self.assertNotIn("Context:", html)

    def test_changes_use_compact_email_safe_tables(self):
        data = {
            "fetched_at": "2026-05-24T16:10:09",
            "source": "https://quantgt.io",
            "monthly": {
                "pick_date": "Unknown",
                "rows": [
                    {
                        "symbol": "AAOI",
                        "company": "Applied Optoelectronics, Inc.",
                        "return": "+97.02%",
                        "gt_score": "4.98/5",
                        "current_price": "$177.62",
                        "buy_or_entry_price": "$90.15",
                        "analyst_signal": "Buy +0.29",
                    }
                ],
            },
            "weekly": {"pick_date": "Week of May 25, 2026", "rows": []},
        }
        diff = {
            "changed": True,
            "monthly": {
                "changed_flag": True,
                "date": None,
                "added": [],
                "removed": [],
                "changed": [
                    {
                        "symbol": "AAOI",
                        "fields": {
                            "analyst_signal": {
                                "old": "Neutral +0.02",
                                "new": "Buy +0.45",
                            }
                        },
                    },
                ],
            },
            "weekly": {
                "changed_flag": False,
                "date": None,
                "added": [],
                "removed": [],
                "changed": [],
            },
        }

        html = build_notification_html(data, diff)

        self.assertNotIn("min-width:980px", html)
        self.assertNotIn("min-width:760px", html)
        self.assertIn('role="presentation"', html)
        self.assertIn("AAOI", html)
        self.assertIn("Analyst Consensus", html)
        self.assertIn("Neutral +0.02", html)
        # one line per field change, not the old Field/Previous/New stacked card
        self.assertNotIn(">Previous<", html)
        self.assertIn("+97.02%", html)
        self.assertIn("entry $90.15", html)
        self.assertNotIn("overflow-x:auto", html)
        self.assertNotIn("min-width:98px", html)

    def test_current_layout_names_and_watchlist_metrics(self):
        data = {
            "fetched_at": "2026-07-13T01:00:00",
            "source": "https://quantgt.io",
            "monthly": {"pick_date": "Updated on July 1, 2026", "rows": []},
            "weekly": {
                "kind": "watchlist",
                "pick_date": "Updated on Jul 10, 2026",
                "rows": [],
            },
        }

        html = build_notification_html(data, None)

        self.assertIn("Portfolio", html)
        self.assertNotIn(">Monthly Picks<", html)

        data["weekly"]["rows"] = [
            {
                "symbol": "SNDK",
                "company": "Sandisk Corporation",
                "sector": "Electronic Technology",
                "gt_score": "4.96/5",
                "current_price": "$1,915.92",
                "buy_or_entry_price": "$618.82",
                "analyst_signal": "Buy +0.24",
            }
        ]

        html = build_notification_html(data, None)

        self.assertIn("Weekly Watchlist", html)
        self.assertIn("4.96", html)
        self.assertIn(" GT", html)
        self.assertIn("buy $618.82", html)
        self.assertIn("Buy +0.24", html)
        self.assertIn("1 stock<", html)


if __name__ == "__main__":
    unittest.main()
