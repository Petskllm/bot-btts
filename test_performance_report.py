import tempfile
import unittest
from pathlib import Path

from performance_report import Stats, add_row, is_published, parse_odds, pct, write_report


class PerformanceReportTests(unittest.TestCase):
    def test_parse_odds_accepts_decimal_comma_and_rejects_invalid_values(self):
        self.assertEqual(parse_odds("2,15"), 2.15)
        self.assertEqual(parse_odds("1.01"), 1.01)
        for value in (None, "", "abc", "1", "0.8", "-2"):
            with self.subTest(value=value):
                self.assertIsNone(parse_odds(value))

    def test_is_published_accepts_only_supported_markers_after_trimming(self):
        for value in ("1", " 1 ", "2"):
            with self.subTest(value=value):
                self.assertTrue(is_published(value))
        for value in (None, "", "0", "true", "3"):
            with self.subTest(value=value):
                self.assertFalse(is_published(value))

    def test_add_row_counts_results_and_prefers_taken_odds(self):
        stats = Stats()
        add_row(stats, {"result": " GAGNÉ ", "odds_taken": "2,50", "odds_market": "4"})
        add_row(stats, {"result": "lost", "odds_taken": "", "odds_market": "3"})
        add_row(stats, {"result": "pending", "odds_market": "5"})
        self.assertEqual((stats.settled, stats.wins, stats.real_odds), (2, 1, 2))
        self.assertAlmostEqual(stats.profit, 0.5)
        self.assertEqual(stats.hit_rate, 0.5)
        self.assertAlmostEqual(stats.roi, 0.25)

    def test_add_row_without_real_odds_keeps_hit_rate_but_no_roi(self):
        stats = Stats()
        add_row(stats, {"result": "won", "odds_taken": "1", "odds_market": "bad"})
        self.assertEqual(stats.hit_rate, 1.0)
        self.assertIsNone(stats.roi)

    def test_empty_stats_and_percent_formatting(self):
        stats = Stats()
        self.assertIsNone(stats.hit_rate)
        self.assertIsNone(stats.roi)
        self.assertEqual(pct(None), "n/a")
        self.assertEqual(pct(0.825), "82.5%")

    def test_write_report_groups_published_rows_and_emits_stable_csv(self):
        rows = [
            {"published": "1", "market": "over15", "result": "won", "odds_taken": "2"},
            {"published": "2", "market": "over15", "result": "lost", "odds_market": "3"},
            {"published": "0", "market": "hidden", "result": "won", "odds_market": "4"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "reports" / "summary.csv"
            write_report(output, rows)
            content = output.read_text(encoding="utf-8")
        self.assertEqual(
            content.splitlines(),
            [
                "type;market;settled;wins;hit_rate;real_odds_n;profit_1u;roi_real;warning",
                "published;over15;2;1;0.500000;2;0.0000;0.000000;echantillon_cotes_reelles_insuffisant",
            ],
        )


if __name__ == "__main__":
    unittest.main()
