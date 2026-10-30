# Adapted from quantcheck offline regression tests (MIT); no legacy runtime imports.
import unittest

from hadan.sources.quantgt_browser import clean_detail_values
from hadan.compat.validation import validate_member_picks_data


def valid_monthly_row(symbol="M1"):
    return {
        "symbol": symbol,
        "company": "Monthly One Inc.",
        "current_price": "$20.00",
        "return": "+12.30%",
        "sector": "Technology",
        "gt_score": "4.50/5",
        "buy_or_entry_price": "$18.00",
        "next_earnings": "2026-06-15",
        "analyst_signal": "Buy +0.25",
    }


def valid_weekly_row(symbol="W1"):
    return {
        "symbol": symbol,
        "company": "Weekly One Inc.",
        "current_price": "$10.00",
        "buy_or_entry_price": "$9.50",
        "sector": "Technology",
        "gt_score": "4.20/5",
        "next_earnings": "2026-06-01",
        "analyst_signal": "Buy +0.20",
    }


def valid_capture():
    return {
        "monthly": {"pick_date": "May 2026", "rows": [valid_monthly_row()]},
        "weekly": {
            "pick_date": "05/22/26",
            "rows": [valid_weekly_row(f"W{i}") for i in range(1, 11)],
        },
    }


def watchlist_weekly(rows, **extra):
    return {
        "kind": "watchlist",
        "pick_date": "Updated on Jun 26, 2026",
        "rows": rows,
        **extra,
    }


def watchlist_card_rows(**extra):
    return [
        {
            "symbol": f"W{i}",
            "company": "Watchlist One Inc.",
            "current_price": "$10.00",
            "sector": "Technology",
            **extra,
        }
        for i in range(1, 11)
    ]


class ValidationTests(unittest.TestCase):
    def test_accepted_captures(self):
        def monthly_without_buy_price(data):
            data["monthly"]["rows"][0]["buy_or_entry_price"] = ""

        def weekly_without_buy_price(data):
            data["weekly"]["rows"][0]["buy_or_entry_price"] = ""

        def new_layout_holdings_date(data):
            data["monthly"]["pick_date"] = "May Holdings 05/01/26 - now"

        def watchlist_dialog_details(data):
            data["weekly"]["kind"] = "watchlist"
            for row in data["weekly"]["rows"]:
                row.update(
                    {
                        "momentum": "1.20/2",
                        "relative_strength": "3.00/3",
                        "gt_score_source": "dialog_components",
                    }
                )

        def watchlist_api_score(data):
            data["weekly"]["kind"] = "watchlist"
            for row in data["weekly"]["rows"]:
                row["gt_score_source"] = "weekly_api_score"

        def watchlist_explicitly_unavailable_details(data):
            watchlist_api_score(data)
            row = data["weekly"]["rows"][0]
            row.update(
                {
                    "next_earnings": "",
                    "next_earnings_unavailable": True,
                    "analyst_signal": "",
                    "analyst_signal_unavailable": True,
                }
            )

        cases = {
            "valid member capture": lambda data: None,
            "monthly detail without buy price when other details loaded": monthly_without_buy_price,
            "weekly detail without buy price when other details loaded": weekly_without_buy_price,
            "new layout without detail rows": new_layout_holdings_date,
            "watchlist with dialog details": watchlist_dialog_details,
            "watchlist api score without retired component fields": watchlist_api_score,
            "watchlist accepts explicitly unavailable source details": watchlist_explicitly_unavailable_details,
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                data = valid_capture()
                mutate(data)
                validate_member_picks_data(data)

    def test_rejected_captures(self):
        def demo_weekly(data):
            data["weekly"]["pick_date"] = "05/15/26"
            data["weekly"]["rows"] = [
                valid_weekly_row(symbol)
                for symbol in ["SNDK", "LITE", "AAOI", "FORM", "VIAV", "ENPH"]
            ]

        def partial_weekly_details(data):
            data["weekly"]["rows"][0]["analyst_signal"] = ""

        def monthly_missing_analyst_signal(data):
            data["monthly"]["rows"][0]["analyst_signal"] = ""

        def monthly_missing_both_fields_entirely(data):
            # Regression (real capture): next_earnings and analyst_signal missing entirely, not just
            # empty, indicates a partial/detail-load failure.
            data["monthly"] = {
                "pick_date": "Updated May 1, 2026",
                "rows": [
                    {
                        "symbol": "AAOI",
                        "company": "Applied Optoelectronics, Inc.",
                        "current_price": "$177.62",
                        "return": "+97.02%",
                        "sector": "Electronic Technology",
                        "gt_score": "4.98/5",
                        "buy_or_entry_price": "$90.15",
                    }
                ],
            }
            data["weekly"] = {
                "pick_date": "Week of May 25, 2026",
                "rows": [
                    {
                        "symbol": f"W{i}",
                        "company": "Weekly One Inc.",
                        "current_price": "$1,589.55",
                        "buy_or_entry_price": "$1,431.67",
                        "sector": "Electronic Technology",
                        "gt_score": "5.01/5",
                        "next_earnings": "Aug 13, 2026",
                        "analyst_signal": "Strong Buy +0.51",
                    }
                    for i in range(1, 11)
                ],
            }

        def monthly_signal_with_description(data):
            data["monthly"]["rows"][0]["analyst_signal"] = (
                "Sell -0.29 Sandisk Corporation develops data storage devices. More Headlines"
            )

        def watchlist_cards_without_dialog_details(data):
            data["monthly"]["pick_date"] = "Updated July 1, 2026"
            data["weekly"] = watchlist_weekly(
                watchlist_card_rows(source_kind="watchlist")
            )

        def watchlist_still_requires_monthly_quality(data):
            data["monthly"]["pick_date"] = "Updated July 1, 2026"
            data["monthly"]["rows"][0]["analyst_signal"] = ""
            data["weekly"] = watchlist_weekly(watchlist_card_rows())

        def partial_top10(data):
            data["weekly"]["rows"] = [valid_weekly_row("BKR")]

        cases = {
            "demo weekly signature": (demo_weekly, "demo Weekly Picks"),
            "partial weekly details": (
                partial_weekly_details,
                "incomplete detail rows",
            ),
            "monthly missing a loaded detail field": (
                monthly_missing_analyst_signal,
                "incomplete loaded rows",
            ),
            "monthly missing detail fields entirely (partial detail load)": (
                monthly_missing_both_fields_entirely,
                "incomplete loaded rows",
            ),
            "monthly analyst signal with company description": (
                monthly_signal_with_description,
                "malformed analyst signal",
            ),
            "watchlist card fields without dialog details": (
                watchlist_cards_without_dialog_details,
                "incomplete detail rows",
            ),
            "watchlist layout still requires monthly detail quality": (
                watchlist_still_requires_monthly_quality,
                "incomplete loaded rows",
            ),
            "partial weekly top 10": (
                partial_top10,
                "expected near-complete Weekly Top 10",
            ),
        }
        for name, (mutate, message) in cases.items():
            with self.subTest(name):
                data = valid_capture()
                mutate(data)
                with self.assertRaisesRegex(RuntimeError, message):
                    validate_member_picks_data(data)

    def test_watchlist_detail_cleaning_preserves_unavailable_marker_types(self):
        cleaned = clean_detail_values(
            {
                "next_earnings": "  —  ",
                "next_earnings_unavailable": True,
                "analyst_signal_unavailable": False,
            }
        )

        self.assertEqual(cleaned["next_earnings"], "—")
        self.assertIs(cleaned["next_earnings_unavailable"], True)
        self.assertIs(cleaned["analyst_signal_unavailable"], False)


if __name__ == "__main__":
    unittest.main()
