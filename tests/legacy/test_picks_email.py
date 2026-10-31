# Adapted from quantcheck offline regression tests (MIT); no legacy runtime imports.
import unittest
from datetime import datetime, timezone


from hadan.compat import picks_email  # noqa: E402
from hadan.compat.diff import compare  # noqa: E402
from hadan.compat.picks_format import (  # noqa: E402
    display_date,
    format_fetched,
    parse_date,
    parse_gt_score,
    parse_money,
    parse_pct,
    short_date,
    since_signal,
    stock_count,
)


def row(symbol, **extra):
    base = {
        "symbol": symbol,
        "company": f"{symbol} Inc.",
        "gt_score": "4.50/5",
        "analyst_signal": "Buy +0.30",
    }
    base.update(extra)
    return base


def snapshot(
    monthly,
    weekly,
    monthly_date="Updated October 1",
    weekly_date="Updated on Oct 2, 2026",
):
    return {
        "fetched_at": "2026-10-06T12:30:54",
        "monthly": {"pick_date": monthly_date, "rows": monthly},
        "weekly": {"pick_date": weekly_date, "kind": "watchlist", "rows": weekly},
    }


class SubjectTests(unittest.TestCase):
    def test_subject_classification(self):
        sep1, sep25 = "Updated September 1", "Updated on Sep 25, 2026"
        cases = {
            "portfolio rebalance lists added and removed symbols": (
                snapshot(
                    [row("HPE"), row("PANW"), row("DELL")],
                    [row("TXG")],
                    monthly_date=sep1,
                ),
                snapshot([row("MRNA"), row("VEEV"), row("DELL")], [row("TXG")]),
                "Quant GT · Portfolio rebalance: +MRNA +VEEV -HPE -PANW",
            ),
            "weekly rotation uses the watchlist name": (
                snapshot([row("DELL")], [row("TXG"), row("HALO")], weekly_date=sep25),
                snapshot([row("DELL")], [row("TXG"), row("U")]),
                "Quant GT · Weekly Watchlist: +U -HALO",
            ),
            "both lists changing mentions both": (
                snapshot([row("A")], [row("B")], monthly_date=sep1, weekly_date=sep25),
                snapshot([row("C")], [row("D")]),
                "Quant GT · Portfolio rebalance: +C -A | Weekly Watchlist: +D -B",
            ),
            "single signal change shows old and new values": (
                snapshot([], [row("U", analyst_signal="Sell -0.20")]),
                snapshot([], [row("U", analyst_signal="Strong Buy +0.60")]),
                "Quant GT · Analyst consensus: U Sell -0.20 → Strong Buy +0.60",
            ),
            "same signal change in both lists counts once": (
                snapshot(
                    [row("U", analyst_signal="Sell -0.20")],
                    [
                        row("U", analyst_signal="Sell -0.20"),
                        row("V", analyst_signal="Buy +0.10"),
                    ],
                ),
                snapshot(
                    [row("U", analyst_signal="Buy +0.60")],
                    [
                        row("U", analyst_signal="Buy +0.60"),
                        row("V", analyst_signal="Buy +0.90"),
                    ],
                ),
                "Quant GT · Analyst consensus changes: U, V",
            ),
            "date-only refresh": (
                snapshot([row("A")], [row("B")], weekly_date=sep25),
                snapshot([row("A")], [row("B")]),
                "Quant GT · Weekly Watchlist refreshed for Oct 2, 2026, no stock changes",
            ),
            "gt score only change": (
                snapshot(
                    [], [row("A", gt_score="4.10/5"), row("B", gt_score="4.20/5")]
                ),
                snapshot(
                    [], [row("A", gt_score="4.30/5"), row("B", gt_score="4.00/5")]
                ),
                "Quant GT · GT Score updated: A B",
            ),
        }
        for name, (old, new, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(
                    picks_email.build_subject(compare(old, new), new), expected
                )

        self.assertEqual(
            picks_email.build_subject(None, snapshot([], [])),
            "Quant GT · Current picks",
        )

    def test_long_symbol_lists_are_truncated(self):
        old = snapshot(
            [row(s) for s in "ABCDEF"], [], monthly_date="Updated September 1"
        )
        new = snapshot([row(s) for s in "GHIJKL"], [])
        subject = picks_email.build_subject(compare(old, new), new)
        self.assertIn("+G +H +I +J +2 more", subject)
        self.assertLess(len(subject), 120)


class BodyTests(unittest.TestCase):
    def setUp(self):
        self.old = snapshot(
            [row("HPE", **{"return": "+18.90%"}), row("DELL")],
            [row("TXG", gt_score="4.79/5")],
            monthly_date="Updated September 1",
        )
        self.new = snapshot(
            [row("MRNA", sector="Health Technology"), row("DELL")],
            [row("TXG", gt_score="4.81/5")],
        )
        self.diff = compare(self.old, self.new)

    def test_html_uses_previous_snapshot_carries_preheader_and_optional_banner(self):
        html = picks_email.build_html(self.new, self.diff, previous=self.old)
        for fragment in (
            "HPE Inc.",
            "Last return",
            "+18.90%",
            ">NEW<",
            "TXG</b>",
            "Portfolio rebalanced",
        ):
            # removed stock details come from the previous snapshot; added stock flagged; GT re-rating chip
            self.assertIn(fragment, html)
        self.assertIn("display:none", html)  # the preheader carries the summary
        self.assertIn("Portfolio rebalance: +MRNA -HPE", html)
        self.assertNotIn("Admin test", html)
        self.assertIn(
            "Admin test",
            picks_email.build_html(self.new, self.diff, banner="Admin test"),
        )

    def test_html_escapes_scraped_text(self):
        new = snapshot([row("X", company="<script>alert(1)</script>")], [])
        html = picks_email.build_html(new, None)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_text_body_lists_changes_and_changed_section_first(self):
        old = snapshot(
            [row("DELL")],
            [row("TXG"), row("HALO")],
            weekly_date="Updated on Sep 25, 2026",
        )
        new = snapshot([row("DELL")], [row("TXG"), row("U")])
        text = picks_email.build_text(new, compare(old, new), previous=old)
        self.assertIn("Changes:", text)
        self.assertIn("+ Added U", text)
        self.assertIn("- Removed HALO", text)
        self.assertLess(
            text.index("Weekly Watchlist — Oct 2, 2026 · 2 stocks"),
            text.index("Portfolio — October 1 · 1 stock"),
        )

    def test_hidden_scrape_metadata_never_reaches_subscribers(self):
        diff = {
            "changed": True,
            "monthly": {"changed_flag": False},
            "weekly": {
                "changed_flag": True,
                "added": [],
                "removed": [],
                "changed": [
                    {
                        "symbol": "A",
                        "fields": {"source_kind": {"old": "", "new": "watchlist"}},
                    },
                ],
            },
        }
        new = snapshot([], [row("A")])
        self.assertNotIn("watchlist</span>", picks_email.build_html(new, diff))
        self.assertNotIn("Source", picks_email.build_text(new, diff))


class FormatTests(unittest.TestCase):
    def test_parsers_and_display_helpers(self):
        self.assertEqual(parse_money("$1,915.92"), 1915.92)
        self.assertEqual(parse_money("$81.10B"), 81.10e9)
        self.assertEqual(parse_money("$616.91M"), 616.91e6)
        self.assertIsNone(parse_money("—"))
        self.assertAlmostEqual(parse_pct("+8.90%"), 0.089)
        self.assertAlmostEqual(parse_pct("-27.62%"), -0.2762)
        self.assertEqual(parse_gt_score("4.61/5"), 4.61)
        self.assertEqual(str(parse_date("Oct 29, 2026")), "2026-10-29")
        self.assertEqual(str(parse_date("2026-10-01")), "2026-10-01")
        self.assertIsNone(parse_date("soon"))
        self.assertEqual(display_date("Updated on Oct 2, 2026"), "Oct 2, 2026")
        self.assertEqual(display_date("Updated October 1"), "October 1")
        self.assertEqual(short_date("Oct 29, 2026"), "Oct 29")
        self.assertEqual(stock_count(1), "1 stock")
        self.assertEqual(stock_count(5), "5 stocks")

    def test_fetched_at_is_naive_utc_shown_in_et_and_beijing(self):
        self.assertEqual(
            format_fetched({"fetched_at": "2026-10-06T12:30:54"}),
            "Oct 6, 2026 · 08:30 ET / 20:30 Beijing",
        )
        self.assertEqual(format_fetched({}), "")


class WatchlistContextTests(unittest.TestCase):
    def _row(self, **extra):
        base = row(
            "TEAM",
            current_price="$196.50",
            signal_price="$187.63",
            signal_date="2026-10-05",
            signal_at="2026-10-05T13:30:00Z",
            watch_reason="Gaining on its sector",
        )
        base.update(extra)
        return base

    def test_since_signal_waits_for_the_signal_time(self):
        r = self._row()
        self.assertIsNone(
            since_signal(r, datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc))
        )
        self.assertAlmostEqual(
            since_signal(r, datetime(2026, 10, 7, 13, 0, tzinfo=timezone.utc)),
            196.50 / 187.63 - 1,
        )
        self.assertIsNone(since_signal(self._row(signal_price="")))

    def test_watchlist_rows_show_reason_and_move_after_signal_but_not_before(self):
        data = snapshot([], [self._row()])
        data["fetched_at"] = "2026-10-07T13:00:00"
        html = picks_email.build_html(data, None)
        self.assertIn("Gaining on its sector", html)
        self.assertIn("+4.7%", html)
        self.assertIn("since Oct 5", html)
        text = picks_email.build_text(data, None)
        self.assertIn("+4.7% since Oct 5", text)
        self.assertIn("Gaining on its sector", text)

        # Weekend detection before the signal shows no move.
        data["fetched_at"] = "2026-10-04T16:00:00"
        html = picks_email.build_html(data, None)
        self.assertNotIn("since Oct 5", html)
        self.assertIn("Gaining on its sector", html)

    def test_watchlist_context_fields_never_trigger_alerts(self):
        old = snapshot(
            [],
            [
                self._row(
                    signal_price="$180.00",
                    watch_reason="",
                    signal_at="",
                    signal_date="",
                )
            ],
        )
        new = snapshot([], [self._row()])
        self.assertFalse(compare(old, new)["changed"])


if __name__ == "__main__":
    unittest.main()


class SignalTimestampSafetyTests(unittest.TestCase):
    def _row(self, signal_at):
        return {
            "signal_at": signal_at,
            "signal_price": "$100.00",
            "current_price": "$110.00",
        }

    def test_ambiguous_or_invalid_signal_times_do_not_render_a_move(self):
        from datetime import datetime, timezone

        as_of = datetime(2026, 10, 7, 13, 0, tzinfo=timezone.utc)
        for value in (None, "", "2026-10-06", "2026-10-06T13:00:00", "not-a-time"):
            with self.subTest(value=value):
                row = self._row(value)
                if value in (None, ""):
                    self.assertAlmostEqual(since_signal(row, as_of), 0.1)
                else:
                    self.assertIsNone(since_signal(row, as_of))

    def test_zoned_signal_times_compare_without_type_errors(self):
        from datetime import datetime, timezone

        as_of = datetime(2026, 10, 7, 13, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(
            since_signal(self._row("2026-10-01T09:00:00Z"), as_of), 0.1
        )
        self.assertIsNone(
            since_signal(self._row("2026-10-08T09:00:00+08:00"), as_of)
        )
