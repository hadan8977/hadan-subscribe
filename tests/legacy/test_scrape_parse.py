# Adapted from quantcheck offline regression tests (MIT); no legacy runtime imports.
"""Regression fixtures built from real Quant GT site changes: keep every input and assertion."""

import unittest

from hadan.sources.quantgt_browser import merge_watchlist_api_scores
from hadan.compat.scrape_parse import (
    extract_pick_date,
    parse_analyst_signal_text,
    parse_watchlist_dialog_text,
    rows_from_card_texts,
    rows_from_matrix,
)

GAMMA_CARD = "Company: Gamma Ltd Symbol: GAMA Sector: Energy Rating: Buy GT Score: 82"


class ScrapeParseTests(unittest.TestCase):
    def test_analyst_signal_text(self):
        cases = {
            "strips company description and headlines": (
                "Sell -0.29 Sandisk Corporation develops data storage devices. More Headlines Stock Market Today",
                "Sell -0.29",
            ),
            "rejects text without a numeric source value": (
                "Sell Sandisk Corporation develops products",
                "",
            ),
        }
        for name, (raw, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(parse_analyst_signal_text(raw), expected)

    def test_rows_from_matrix_for_each_table_header_generation(self):
        cases = {
            "new monthly header": (
                "monthly",
                [
                    [
                        "Company",
                        "Symbol",
                        "Held Since",
                        "Price",
                        "Return",
                        "Sector",
                        "Rating",
                        "GT Score",
                    ],
                    [
                        "Acme Corp",
                        "ACME",
                        "05/01/26",
                        "$123.45",
                        "+12.5%",
                        "Technology",
                        "Buy",
                        "87",
                    ],
                    [
                        "$123.45 P/E (TTM) 20 Market Cap $10B",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                    ],
                ],
                [
                    {
                        "company": "Acme Corp",
                        "symbol": "ACME",
                        "held_since": "05/01/26",
                        "current_price": "$123.45",
                        "return": "+12.5%",
                        "sector": "Technology",
                        "rating": "Buy",
                        "gt_score": "87",
                    }
                ],
            ),
            "new weekly header": (
                "weekly",
                [
                    ["Company", "Symbol", "Sector", "Rating", "GT Score"],
                    ["Beta Inc", "BETA", "Healthcare", "Strong Buy", "91"],
                ],
                [
                    {
                        "company": "Beta Inc",
                        "symbol": "BETA",
                        "sector": "Healthcare",
                        "rating": "Strong Buy",
                        "gt_score": "91",
                    }
                ],
            ),
            "logged-in monthly header": (
                "monthly",
                [
                    [
                        "COMPANY",
                        "SYMBOL",
                        "HELD SINCE",
                        "PRICE",
                        "RETURN",
                        "SECTOR",
                        "RATING",
                        "GT SCORE",
                        "",
                    ],
                    [
                        "Applied Optoelectronics, Inc.",
                        "AAOI",
                        "2026-04-01",
                        "$181.49",
                        "+101.31%",
                        "Electronic Technology",
                        "Strong Buy",
                        "4.98/5",
                        "",
                    ],
                ],
                [
                    {
                        "company": "Applied Optoelectronics, Inc.",
                        "symbol": "AAOI",
                        "held_since": "2026-04-01",
                        "current_price": "$181.49",
                        "return": "+101.31%",
                        "sector": "Electronic Technology",
                        "rating": "Strong Buy",
                        "gt_score": "4.98/5",
                    }
                ],
            ),
            "current weekly table without a rating column": (
                "weekly",
                [
                    ["COMPANY", "SYMBOL", "PRICE", "SECTOR", "GT SCORE", ""],
                    [
                        "Sandisk Corporation",
                        "SNDK",
                        "$1,431.67",
                        "Electronic Technology",
                        "5.01/5",
                        "",
                    ],
                ],
                [
                    {
                        "company": "Sandisk Corporation",
                        "symbol": "SNDK",
                        "buy_or_entry_price": "$1,431.67",
                        "sector": "Electronic Technology",
                        "gt_score": "5.01/5",
                    }
                ],
            ),
        }
        for name, (kind, matrix, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(rows_from_matrix(matrix, kind), expected)

        # Quant GT renamed the Portfolio holdings table's "Held Since" column to "Entry Date";
        # the internal field name stays held_since.
        matrix = [
            [
                "SYMBOL",
                "COMPANY",
                "ENTRY DATE",
                "PRICE",
                "RETURN",
                "SECTOR",
                "GT SCORE",
            ],
            [
                "DELL",
                "Dell Technologies Inc.",
                "2026-08-03",
                "$456.24",
                "+14.73%",
                "Electronic Technology",
                "4.78/5",
            ],
        ]
        self.assertEqual(
            rows_from_matrix(matrix, "monthly")[0]["held_since"], "2026-08-03"
        )

    def test_rows_from_card_texts(self):
        gamma = {
            "company": "Gamma Ltd",
            "symbol": "GAMA",
            "sector": "Energy",
            "rating": "Buy",
            "gt_score": "82",
        }
        cases = {
            "card layout": ([GAMMA_CARD], gamma),
            # A legacy weekly card with a GT score is not treated as a priceless watchlist card.
            "legacy weekly card is not a watchlist card": ([GAMMA_CARD], gamma),
            "watchlist card": (
                ["SNDK Sandisk Corporation $2,032.22 Electronic Technology"],
                {
                    "symbol": "SNDK",
                    "company": "Sandisk Corporation",
                    "current_price": "$2,032.22",
                    "sector": "Electronic Technology",
                    "source_kind": "watchlist",
                },
            ),
            # Quant GT's Watchlist card stopped rendering an inline $price; only the symbol is safely
            # extractable, and the row is still tagged watchlist so downstream code enriches it from
            # the authenticated Watchlist API instead of rejecting it outright.
            "watchlist card without a price": (
                ["CORT Corcept Therapeutics Incorporated Health Technology"],
                {"symbol": "CORT", "source_kind": "watchlist"},
            ),
        }
        for name, (cards, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(rows_from_card_texts(cards, "weekly"), [expected])

        # "Analyst Consensus" is a detail-label boundary: it must not leak into the sector.
        cards = [
            "Company: Gamma Ltd Symbol: GAMA Sector: Energy Analyst Consensus Buy +0.12 Momentum 1.9/2 GT Score: 82"
        ]
        self.assertEqual(rows_from_card_texts(cards, "weekly")[0]["sector"], "Energy")

    def test_watchlist_dialog_text(self):
        # Regression (2026-09-08, APGE): when Momentum/Relative Strength have no data, Quant GT renders
        # no label text for them at all. value_after's boundary search for "Analyst Consensus" then has
        # no nearby label to stop at and ran all the way to "More Headlines", swallowing the whole
        # company-description paragraph, so analyst_signal_unavailable was False and the stock looked
        # like a genuine scrape failure ("missing analyst_signal") every run for days.
        apge = (
            "APGE Health Technology Apogee Therapeutics, Inc. PRICE $135.07 — "
            "1M 6M 1Y YTD P/E (TTM) — Market Cap $10.22B Revenue (TTM) — "
            "Revenue Growth (YoY) — Next Earnings — Analyst Consensus — "
            "Apogee Therapeutics, Inc. develops and commercializes biologic "
            "therapies for immunological and inflammatory (I&I) diseases for "
            "patients and caregivers. The company's pipeline includes "
            "products, such as ZUMILOKIBART (APG777), a monoclonal antibody "
            "for atopic dermatitis, asthma, and eosinophilic… More Headlines Close"
        )
        unavailable = {
            "next_earnings": "",
            "next_earnings_unavailable": True,
            "analyst_signal": "",
            "analyst_signal_unavailable": True,
        }
        cases = {
            "restores legacy weekly fields": (
                "SNDK",
                "SNDK Electronic Technology Sandisk Corporation PRICE $1,915.92 "
                "$1915.92 +596.09% 1M 6M 1Y YTD SNDK : $618.82 "
                "P/E (TTM) 66.60 Market Cap $283.21B Revenue (TTM) $13.18B "
                "Revenue Growth (YoY) +82.76% Next Earnings Aug 13, 2026 "
                "Analyst Consensus Buy +0.24 Momentum 1.96/2 Relative Strength 3.00/3 "
                "Sandisk Corporation develops data storage products. More Headlines",
                {
                    "buy_or_entry_price": "$618.82",
                    "market_cap": "$283.21B",
                    "next_earnings": "Aug 13, 2026",
                    "analyst_signal": "Buy +0.24",
                    "momentum": "1.96/2",
                    "relative_strength": "3.00/3",
                    "gt_score": "4.96/5",
                },
                (),
            ),
            "current layout extracts consensus without retired scores": (
                "SNDK",
                "SNDK Electronic Technology Sandisk Corporation PRICE $1,505.00 "
                "SNDK : $702.49 P/E (TTM) 55.98 Market Cap $237.91B "
                "Revenue (TTM) $13.18B Revenue Growth (YoY) +82.76% "
                "Next Earnings Aug 5, 2026 Analyst Consensus Neutral -0.09 "
                "Sandisk Corporation develops data storage products. More Headlines Close",
                {"next_earnings": "Aug 5, 2026", "analyst_signal": "Neutral -0.09"},
                ("gt_score",),
            ),
            "marks source-unavailable details": (
                "CORT",
                "CORT Health Technology Corcept Therapeutics Incorporated PRICE $90.89 "
                "P/E (TTM) — Market Cap $10.25B Revenue (TTM) — "
                "Revenue Growth (YoY) — Next Earnings — Analyst Consensus — "
                "Headlines Corcept Therapeutics lifted its outlook Close",
                unavailable,
                (),
            ),
            "unavailable analyst consensus is not swallowed by the company blurb (APGE)": (
                "APGE",
                apge,
                unavailable,
                (),
            ),
        }
        for name, (symbol, text, expected, absent) in cases.items():
            with self.subTest(name):
                details = parse_watchlist_dialog_text(text, symbol)
                self.assertEqual({k: details[k] for k in expected}, expected)
                for key in absent:
                    self.assertNotIn(key, details)

    def test_merge_watchlist_api_scores(self):
        merged = merge_watchlist_api_scores(
            [{"symbol": "SNDK"}, {"symbol": "MXL"}],
            [{"ticker": "SNDK", "score": 4.8592}, {"ticker": "MXL", "score": 4.8452}],
        )
        self.assertEqual(
            (
                merged[0]["gt_score"],
                merged[0]["gt_score_source"],
                merged[1]["gt_score"],
            ),
            ("4.86/5", "weekly_api_score", "4.85/5"),
        )

        merged = merge_watchlist_api_scores(
            [{"symbol": "CORT", "source_kind": "watchlist"}],
            [
                {
                    "ticker": "CORT",
                    "name": "Corcept Therapeutics Incorporated",
                    "sector": "Health Technology",
                    "price": None,
                    "sell_price": 113.88,
                    "score": 4.7075,
                }
            ],
        )
        self.assertEqual(
            {
                k: merged[0][k]
                for k in ("company", "sector", "current_price", "gt_score")
            },
            {
                "company": "Corcept Therapeutics Incorporated",
                "sector": "Health Technology",
                "current_price": "$113.88",
                "gt_score": "4.71/5",
            },
        )

        merged = merge_watchlist_api_scores(
            [
                {
                    "symbol": "SNDK",
                    "company": "Card Company",
                    "sector": "Card Sector",
                    "current_price": "$2,032.22",
                }
            ],
            [
                {
                    "ticker": "SNDK",
                    "name": "API Company",
                    "sector": "API Sector",
                    "sell_price": 1.0,
                    "score": 4.5,
                }
            ],
        )
        self.assertEqual(
            {
                k: merged[0][k] for k in ("company", "sector", "current_price")
            },  # card-supplied fields win
            {
                "company": "Card Company",
                "sector": "Card Sector",
                "current_price": "$2,032.22",
            },
        )

    def test_extract_pick_date(self):
        cases = {
            "monthly holdings date": (
                "monthly",
                "Portfolio Return May Holdings 05/01/26 - now Company Symbol Held Since",
                "May Holdings 05/01/26 - now",
            ),
            "monthly updated date": (
                "monthly",
                "Portfolio Return Latest Holdings Updated May 1, 2026 MTD +14.76% COMPANY SYMBOL",
                "Updated May 1, 2026",
            ),
            "monthly updated-on date": (
                "monthly",
                "Portfolio Return Latest Holdings Updated on July 1, 2026 MTD -4.38% COMPANY SYMBOL",
                "Updated on July 1, 2026",
            ),
            # Current Portfolio page drops the year/comma and glues the MTD badge onto the day number.
            "monthly updated date without year": (
                "monthly",
                "Latest holdings Updated August 1MTD +6.25% SYMBOL COMPANY ENTRY DATE PRICE",
                "Updated August 1",
            ),
            "weekly week-of date": (
                "weekly",
                "Weekly Picks Guidance only Week of May 25, 2026 COMPANY SYMBOL SECTOR",
                "Week of May 25, 2026",
            ),
            "weekly updated-on abbreviated date": (
                "weekly",
                "Weekly Picks Guidance only Updated on Jun 5, 2026 WTD return +0.00% COMPANY SYMBOL",
                "Updated on Jun 5, 2026",
            ),
            "weekly updated abbreviated date": (
                "weekly",
                "Weekly Picks Updated Jun 5, 2026 COMPANY SYMBOL",
                "Updated Jun 5, 2026",
            ),
        }
        for name, (kind, text, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(extract_pick_date(text, kind), expected)


if __name__ == "__main__":
    unittest.main()
