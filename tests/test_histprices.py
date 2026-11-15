import datetime as dt
import unittest
from unittest import mock

from openclaw import config, histprices, histstore
from openclaw.histprices import CLOSE, OPEN, PriceSeries

from .helpers import fresh_research_db, payload, weekdays

D = dt.date
START = D(2026, 8, 3)       # a Monday, clear of ASX holidays for 9 weeks


class AdjustmentTests(unittest.TestCase):
    def test_adjusted_prices_match_auto_adjust(self):
        days = weekdays(START, 3)
        s = PriceSeries("ZZZ", payload(days, [2.00, 2.10, 2.20],
                                       opens=[1.90, 2.05, 2.15],
                                       adj_factor=0.95))
        # yfinance auto_adjust: close becomes Adj Close, open is scaled by
        # the same ratio.
        self.assertAlmostEqual(s.adjusted(1, CLOSE), 2.10 * 0.95)
        self.assertAlmostEqual(s.adjusted(1, OPEN), 2.05 * 0.95)

    def test_quoted_price_unwinds_a_consolidation(self):
        """1-for-20 consolidation effective on the 6th session. Yahoo's
        history shows 0.44 throughout; the screen showed 2.2c before it."""
        days = weekdays(START, 10)
        s = PriceSeries("ZZZ", payload(days, [0.44] * 10,
                                       splits=[(days[5], 0.05)]))
        self.assertAlmostEqual(s.quoted(4, CLOSE), 0.022)
        self.assertAlmostEqual(s.quoted(5, CLOSE), 0.44)
        self.assertAlmostEqual(s.quoted(9, CLOSE), 0.44)
        # returns are unaffected: the adjusted series is continuous
        self.assertAlmostEqual(s.adjusted(4, CLOSE), s.adjusted(5, CLOSE))

    def test_tick_sizes(self):
        self.assertEqual(histprices.tick_size(0.007), 0.001)
        self.assertEqual(histprices.tick_size(0.0999), 0.001)
        self.assertEqual(histprices.tick_size(0.10), 0.005)
        self.assertEqual(histprices.tick_size(1.995), 0.005)
        self.assertEqual(histprices.tick_size(2.00), 0.01)
        self.assertEqual(histprices.tick_size(58.86), 0.01)

    def test_bad_rows_are_dropped_not_trusted(self):
        days = weekdays(START, 3)
        p = payload(days, [1.0, 1.1, 1.2])
        p["bars"][1][4] = 0.0                    # a zero close
        p["bars"].append(["not-a-date", 1, 1, 1, 1, 1, 1])
        s = PriceSeries("ZZZ", p)
        self.assertEqual(s.dates, [days[0], days[2]])


class MarketCapTests(unittest.TestCase):
    def test_uses_the_share_count_of_the_day(self):
        """Shares treble through a placement on the 11th session. Sizing an
        early announcement with today's share count would be 3x too big."""
        days = weekdays(START, 20)
        s = PriceSeries("ZZZ", payload(
            days, [0.10] * 20,
            shares=[(days[0], 100e6), (days[10], 300e6)]))
        self.assertAlmostEqual(s.market_cap_before(days[6]), 10e6)
        self.assertAlmostEqual(s.market_cap_before(days[19]), 30e6)

    def test_never_looks_at_the_announcement_day(self):
        """The price quintuples on the announcement. The size screen must
        see the company as it was the night before."""
        days = weekdays(START, 12)
        closes = [0.10] * 8 + [0.50] * 4
        s = PriceSeries("ZZZ", payload(days, closes,
                                       shares=[(days[0], 200e6)]))
        self.assertAlmostEqual(s.market_cap_before(days[8]), 20e6)
        self.assertAlmostEqual(s.market_cap_before(days[9]), 20e6)  # median
        self.assertAlmostEqual(s.market_cap_before(days[11]), 100e6)

    def test_share_feed_lagging_a_split_does_not_spike(self):
        """Seen live on WCN, Oct 2026: split dated the 28th, the share count
        only stepped down on the 29th. Unhandled, the 28th shows a company
        twenty times its real size."""
        days = weekdays(START, 14)
        s = PriceSeries("ZZZ", payload(
            days, [0.44] * 14,
            splits=[(days[5], 0.05)],
            shares=[(days[0], 3_956_718_305), (days[6], 197_835_915)]))
        for i in range(2, 14):
            cap = s.market_cap_before(days[i])
            self.assertAlmostEqual(cap / 1e6, 87.05, places=1,
                                   msg=f"session {i}")

    def test_quoted_price_and_reported_shares_agree_across_a_split(self):
        days = weekdays(START, 12)
        s = PriceSeries("ZZZ", payload(
            days, [0.44] * 12, splits=[(days[5], 0.05)],
            shares=[(days[0], 4_000_000_000), (days[5], 200_000_000)]))
        self.assertAlmostEqual(s.market_cap_before(days[3]), 88e6)
        self.assertAlmostEqual(s.market_cap_before(days[11]), 88e6)

    def test_no_share_history_is_unknown_not_a_guess(self):
        days = weekdays(START, 6)
        s = PriceSeries("ZZZ", payload(days, [0.10] * 6))
        self.assertIsNone(s.market_cap_before(days[5]))

    def test_no_prior_session_is_unknown(self):
        days = weekdays(START, 6)
        s = PriceSeries("ZZZ", payload(days, [0.10] * 6,
                                       shares=[(days[0], 1e8)]))
        self.assertIsNone(s.market_cap_before(days[0]))

    def test_before_the_first_share_figure_uses_the_earliest(self):
        days = weekdays(START, 10)
        s = PriceSeries("ZZZ", payload(days, [0.10] * 10,
                                       shares=[(days[6], 150e6)]))
        self.assertEqual(s.shares_asof(days[2]), 150e6)
        self.assertAlmostEqual(s.market_cap_before(days[3]), 15e6)

    def test_missing_consolidation_is_flagged(self):
        days = weekdays(START, 12)
        closes = [0.02] * 6 + [0.40] * 6     # raw 20x jump, no split on file
        missing = PriceSeries("ZZZ", payload(
            days, closes, shares=[(days[0], 4e9), (days[6], 2e8)]))
        self.assertTrue(
            missing.discontinuity_between(days[2], days[9]))
        self.assertFalse(
            missing.discontinuity_between(days[7], days[11]))

        recorded = PriceSeries("ZZZ", payload(
            days, [0.40] * 12, splits=[(days[6], 0.05)],
            shares=[(days[0], 4e9), (days[6], 2e8)]))
        self.assertFalse(
            recorded.discontinuity_between(days[2], days[9]))

        placement = PriceSeries("ZZZ", payload(
            days, [0.10] * 12, shares=[(days[0], 1e8), (days[6], 3e8)]))
        self.assertFalse(
            placement.discontinuity_between(days[2], days[9]))


class UnappliedSplitTests(unittest.TestCase):
    """Yahoo records some splits without restating the older prices."""

    def test_a_split_that_never_reached_the_prices_is_found(self):
        """JNS, Mar 2025: a 1-for-200 consolidation recorded on 2 April, but
        the price level had already jumped 200x some days before that."""
        days = weekdays(D(2026, 8, 3), 40)
        closes = [0.001] * 20 + [0.20] * 20          # raw 200x jump, day 20
        s = PriceSeries("ZZZ", payload(days, closes,
                                       splits=[(days[26], 0.005)]))
        self.assertEqual(s.unapplied_splits, [days[20]])
        self.assertTrue(s.discontinuity_between(days[15], days[25]))
        self.assertTrue(s.discontinuity_between(days[20], days[30]))
        self.assertFalse(s.discontinuity_between(days[21], days[35]))
        self.assertFalse(s.discontinuity_between(days[2], days[19]))

    def test_a_properly_applied_split_is_not_flagged(self):
        days = weekdays(D(2026, 8, 3), 40)
        s = PriceSeries("ZZZ", payload(days, [0.40] * 40,
                                       splits=[(days[20], 0.05)]))
        self.assertEqual(s.unapplied_splits, [])
        self.assertFalse(s.discontinuity_between(days[10], days[30]))

    def test_a_forward_split_that_was_not_applied_is_found(self):
        days = weekdays(D(2026, 8, 3), 40)
        closes = [1.0] * 20 + [0.5] * 20             # raw halving, 2-for-1
        s = PriceSeries("ZZZ", payload(days, closes,
                                       splits=[(days[20], 2.0)]))
        self.assertEqual(s.unapplied_splits, [days[20]])

    def test_a_real_move_far_from_any_split_is_left_alone(self):
        days = weekdays(D(2026, 8, 3), 60)
        closes = [0.02] * 10 + [0.10] * 50           # genuine 5x, no split
        s = PriceSeries("ZZZ", payload(days, closes))
        self.assertEqual(s.unapplied_splits, [])
        self.assertFalse(s.discontinuity_between(days[5], days[40]))

    def test_a_dividend_does_not_trigger_it(self):
        days = weekdays(D(2026, 8, 3), 40)
        s = PriceSeries("ZZZ", payload(days, [1.0] * 40, adj_factor=0.9,
                                       splits=[(days[20], 0.5)]))
        # Close is flat, so a split recorded here has nothing to explain
        self.assertEqual(s.unapplied_splits, [])


class LoadManyTests(unittest.TestCase):
    def setUp(self):
        fresh_research_db()
        self.days = weekdays(START, 5)
        self.calls = []

    def fake_fetch(self, ticker, start, end, attempts=4):
        self.calls.append(ticker)
        if ticker == "GONE":
            return "no_data", None
        if ticker == "FLAKY":
            return "error", None
        return "ok", payload(self.days, [1.0] * 5,
                             shares=[(self.days[0], 1e8)])

    def load(self, tickers, start=START, end=D(2026, 8, 7), **kw):
        with mock.patch.object(histprices, "_fetch_one", self.fake_fetch):
            return histprices.load_many(tickers, start, end,
                                        progress=lambda m: None, **kw)

    def test_results_and_statuses(self):
        out, status = self.load(["AAA", "GONE", "FLAKY"])
        self.assertEqual(len(out["AAA"]), 5)
        self.assertIsNone(out["GONE"])
        self.assertIsNone(out["FLAKY"])
        self.assertEqual(status, {"AAA": "ok", "GONE": "no_data",
                                  "FLAKY": "error"})

    def test_second_run_only_refetches_the_failure(self):
        """A missing ticker is remembered. A failed download is not, or one
        bad afternoon at Yahoo would silently shrink the universe forever."""
        self.load(["AAA", "GONE", "FLAKY"])
        self.calls.clear()
        out, status = self.load(["AAA", "GONE", "FLAKY"])
        self.assertEqual(self.calls, ["FLAKY"])
        self.assertEqual(len(out["AAA"]), 5)
        self.assertEqual(status["GONE"], "no_data")

    def test_a_wider_range_is_a_cache_miss(self):
        self.load(["AAA"])
        self.calls.clear()
        self.load(["AAA"], end=D(2026, 8, 20))
        self.assertEqual(self.calls, ["AAA"])

    def test_a_range_that_moved_on_a_few_days_is_still_a_hit(self):
        """The study asks for prices up to yesterday, which moves daily. A
        cache that missed on every new date re-downloaded ~1,900 tickers
        each morning."""
        self.load(["AAA"], end=D(2026, 8, 7))
        self.calls.clear()
        slack = config.STUDY_PRICE_CACHE_SLACK_DAYS
        self.load(["AAA"], end=D(2026, 8, 7) + dt.timedelta(days=slack))
        self.assertEqual(self.calls, [])
        self.load(["AAA"], end=D(2026, 8, 7) + dt.timedelta(days=slack + 1))
        self.assertEqual(self.calls, ["AAA"])

    def test_an_earlier_start_is_a_cache_miss(self):
        self.load(["AAA"])
        self.calls.clear()
        self.load(["AAA"], start=START - dt.timedelta(days=10))
        self.assertEqual(self.calls, ["AAA"])

    def test_refresh_ignores_the_cache(self):
        self.load(["AAA"])
        self.calls.clear()
        self.load(["AAA"], refresh=True)
        self.assertEqual(self.calls, ["AAA"])

    def test_cache_round_trip_is_lossless(self):
        self.load(["AAA"])
        hit = histstore.price_cache_get("AAA")
        self.assertEqual(hit["status"], "ok")
        self.assertEqual(hit["payload"]["bars"][0][0], START.isoformat())
        self.assertEqual(hit["payload"]["shares"],
                         [[START.isoformat(), 1e8]])

    def test_missing_ticker_classification(self):
        class YFTzMissingError(Exception):
            pass
        self.assertTrue(histprices._is_missing_ticker(YFTzMissingError("x")))
        self.assertTrue(histprices._is_missing_ticker(
            Exception("HTTP Error 404: Not Found")))
        self.assertFalse(histprices._is_missing_ticker(
            Exception("Too Many Requests. Rate limited.")))
        self.assertFalse(histprices._is_missing_ticker(TimeoutError("slow")))


if __name__ == "__main__":
    unittest.main()
