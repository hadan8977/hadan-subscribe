"""Offline presentation and source-structure regression fixtures (quantcheck MIT)."""

import unittest
from hadan.compat.site_diff import diff


def page(name, headings=(), nav=(), buttons=(), links=()):
    return {
        "name": name,
        "headings": list(headings),
        "nav": list(nav),
        "buttons": list(buttons),
        "links": [{"text": t, "href": h} for t, h in links],
    }


def member_page(
    name, headings=(), nav=("Portfolio", "Weekly Digest"), buttons=(), links=()
):
    return page(name, headings, nav, buttons, links)


def member_snapshot(*pages):
    return {"capture_mode": "fresh_login_member", "pages": list(pages)}


class SiteDiffNoiseTests(unittest.TestCase):
    def week(self, cards, pct, headline, archive, article):
        return member_snapshot(
            member_page("weekly", ["Watchlist"], buttons=["U", *cards]),
            member_page(
                "track_record", ["Track Record", "Monthly returns"], buttons=["U", *pct]
            ),
            member_page(
                "weekly_digest",
                ["Weekly Digest", headline, "Sector rotation"],
                buttons=["U", "Browse the archive", *archive],
                links=[
                    ("01 The news", "https://quantgt.io/weekly-digest#digest-take"),
                    ("Some headline", "https://www.cnn.com/2026/10/01/story"),
                ],
            ),
            member_page(
                "research",
                ["Quant Research", article],
                buttons=["U", "ALL 12" if "1%" in article else "ALL 13"],
                links=[(article, "https://quantgt.io/research/" + article[:5])],
            ),
        )

    def test_noise_and_baseline_switches_produce_no_site_change_alert(self):
        old_week = self.week(
            ["TXG 10x Genomics, Inc. Health Technology"],
            ["+17.4%", "+2.1%"],
            "Hiring stalled",
            ["SEP 25 Bond yields hit 5%"],
            "Why Are Only 1% of Day Traders Profitable?",
        )
        new_week = self.week(
            ["NVDA NVIDIA Corporation Electronic Technology"],
            ["+17.4%", "+3.0%"],
            "Fed cuts rates",
            ["OCT 2 Hiring stalled"],
            "A brand new article",
        )
        placeholder = {
            "pages": [
                member_page(
                    "weekly",
                    ["Watchlist"],
                    buttons=["U", "Subscribe", "SN SNDK Sandisk Holdings Technology"],
                )
            ]
        }
        member = member_snapshot(
            member_page(
                "weekly",
                ["Watchlist", "Portfolio candidate"],
                buttons=["U", "TXG 10x Genomics"],
            ),
            member_page(
                "live_update", ["Live Update"], buttons=["All", "Live Insights"]
            ),
        )
        cases = {
            "capture failure suppresses the alert": (
                {"pages": [page("dashboard", ["Old"])]},
                {"pages": [{"name": "dashboard", "capture_warning": "Timeout"}]},
            ),
            "market tools news noise": (
                {"pages": [page("market_tools", ["Tools"])]},
                {
                    "pages": [
                        page(
                            "market_tools",
                            ["Tools", "Breaking news"],
                            links=[("News", "https://www.cnbc.com/story")],
                        )
                    ]
                },
            ),
            "weekly data rotation (cards, returns, headlines, archive, articles)": (
                old_week,
                new_week,
            ),
            # Switching from a placeholder (logged-out) capture to a member capture is a baseline, not an alert.
            "switch from placeholder capture": (placeholder, member),
        }
        for name, (old, new) in cases.items():
            with self.subTest(name):
                self.assertEqual(diff(old, new), [])

    def test_news_ticker_links_are_suppressed_but_real_nav_change_surfaces(self):
        old = {
            "pages": [
                page(
                    "dashboard",
                    nav=["Research NEW"],
                    links=[
                        ("Research NEW", "https://quantgt.io/research"),
                        (
                            "BofA Raises SanDisk (SNDK) Price Target 11h ago",
                            "https://finance.yahoo.com/markets/stocks/articles/x",
                        ),
                    ],
                )
            ]
        }
        new = {
            "pages": [
                page(
                    "dashboard",
                    nav=["Quant Research NEW"],
                    links=[
                        ("Quant Research NEW", "https://quantgt.io/research"),
                        (
                            "Dow Jones Futures: Stock Market Jumps 7m ago",
                            "https://finance.yahoo.com/m/abc",
                        ),
                    ],
                )
            ]
        }

        lines = diff(old, new)
        # The genuine nav rename must surface.
        self.assertIn("dashboard nav added: Quant Research NEW", lines)
        self.assertIn("dashboard nav removed: Research NEW", lines)
        # No news-ticker link (Yahoo Finance / "N ago" timestamps) may appear anywhere.
        joined = "\n".join(lines)
        self.assertNotIn("finance.yahoo.com", joined)
        self.assertNotIn("ago", joined)

    def test_real_structure_changes_still_surface(self):
        args = (
            ["TXG 10x Genomics"],
            ["+1.0%"],
            "H",
            ["SEP 25 X"],
            "Why Are Only 1% of Day Traders Profitable?",
        )
        base, changed = self.week(*args), self.week(*args)
        changed["pages"][0]["buttons"].append("Export CSV")  # new watchlist feature
        changed["pages"][2]["links"].pop(0)  # digest section removed
        changed["pages"][3]["nav"] = ["Portfolio"]  # nav item removed
        lines = diff(base, changed)
        self.assertTrue(
            any("weekly buttons added: Export CSV" in line for line in lines), lines
        )
        self.assertTrue(
            any("weekly_digest links removed" in line for line in lines), lines
        )
        self.assertTrue(
            any("research nav removed: Weekly Digest" in line for line in lines), lines
        )
