import datetime as dt
import math
import os
import unittest
from unittest import mock

from openclaw import (config, eventstudy, filters, histprices, histstore,
                      prices, shadowbook, store)
from openclaw.histprices import PriceSeries
from openclaw.marketstate import classify, parse_feed_timestamp

from .helpers import corpus_row, fresh_research_db, payload, weekdays

D = dt.date
START = D(2026, 8, 3)       # a Monday, clear of ASX holidays for 9 weeks
DAYS = weekdays(START, 45)


def wiggle(base, i, step):
    """Deterministic, non-monotonic prices so a wrong bar gives a wrong
    answer instead of an accidentally right one."""
    return base * (1.0 + step * ((i * 7) % 11 - 5))


STOCK = payload(DAYS,
                closes=[wiggle(1.00, i, 0.013) for i in range(45)],
                opens=[wiggle(1.00, i, 0.013) * (1 + 0.004 * ((i * 5) % 7 - 3))
                       for i in range(45)],
                adj_factor=0.97,
                shares=[(DAYS[0], 30e6)])
BENCH = payload(DAYS, closes=[wiggle(3000.0, i, 0.002) for i in range(45)])


class ShadowBookParityTests(unittest.TestCase):
    """The study must price a trade exactly as the forward book does, or
    comparing the two (the whole point of the gate) means nothing.

    The live book runs here for real, against its own ledger code, with only
    the network layer replaced."""

    def setUp(self):
        store.init()

        def live_bars(ticker, start, end, attempts=3):
            # what yfinance hands the live book with auto_adjust=True
            out = {}
            for b in STOCK["bars"]:
                day = D.fromisoformat(b[0])
                if start <= day < end:
                    f = b[5] / b[4]
                    out[day] = {"open": b[1] * f, "high": b[2] * f,
                                "low": b[3] * f, "close": b[5],
                                "volume": b[6]}
            return out

        bench = {D.fromisoformat(b[0]): b[5] for b in BENCH["bars"]}
        for target, name, value in [
            (prices, "daily_bars", live_bars),
            (prices, "benchmark_series", lambda s, e: ("^AXSO", bench)),
            (prices, "_today_syd", lambda: D(2026, 10, 30)),
            (shadowbook, "_today_syd",
             lambda: dt.datetime(2026, 10, 30, 17, 0)),
            (shadowbook, "bar_is_final", lambda day, field: True),
        ]:
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def check(self, raw_timestamp, mcap, expect_field, expect_entry_day):
        published = parse_feed_timestamp(raw_timestamp)
        key = f"PARITY-{raw_timestamp}-{mcap}"
        rec = {"doc_key": key, "ticker": "ZZZ", "headline": "Test",
               "published_raw": raw_timestamp,
               "market_state": classify(published)}
        result = filters.Result(True, "ok", "DRILLING_RESULTS", mcap, 5000, 0)
        trade_id = shadowbook.record_decision(rec, result)
        shadowbook.settle()

        trade = store.connect().execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        legs = {r["horizon"]: r for r in store.legs_for_trade(trade_id)}
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(set(legs), set(config.HORIZONS))

        sim = eventstudy.simulate(
            PriceSeries("ZZZ", STOCK), PriceSeries("^AXSO", BENCH),
            published, filters.slippage_haircut(mcap))

        self.assertEqual(sim["entry_field"], expect_field)
        self.assertEqual(sim["entry_day"], expect_entry_day)
        self.assertEqual(trade["entry_field"], sim["entry_field"])
        self.assertEqual(trade["entry_bar_date"],
                         sim["entry_day"].isoformat())
        self.assertAlmostEqual(trade["entry_price_raw"], sim["entry_price"],
                               places=12)
        self.assertAlmostEqual(trade["haircut"], sim["haircut"])
        for h in config.HORIZONS:
            self.assertEqual(legs[h]["exit_bar_date"],
                             sim["legs"][h]["exit_day"].isoformat())
            self.assertAlmostEqual(legs[h]["ret_pct"], sim["legs"][h]["ret"],
                                   places=9)
            self.assertAlmostEqual(legs[h]["bench_ret_pct"],
                                   sim["legs"][h]["bench_ret"], places=9)
            self.assertAlmostEqual(legs[h]["excess_pct"],
                                   sim["legs"][h]["excess"], places=9)

    # Sydney is UTC+10 in August. 5 Aug 2026 is a Wednesday.
    def test_pre_open_enters_at_that_days_open(self):
        self.check("2026-08-04T23:30:00.000Z", 30e6, "open", D(2026, 8, 5))

    def test_opening_auction_enters_at_that_days_close(self):
        self.check("2026-08-05T00:05:00.000Z", 30e6, "close", D(2026, 8, 5))

    def test_continuous_enters_at_that_days_close(self):
        self.check("2026-08-05T02:00:00.000Z", 30e6, "close", D(2026, 8, 5))

    def test_closing_auction_enters_at_the_next_open(self):
        self.check("2026-08-05T06:05:00.000Z", 30e6, "open", D(2026, 8, 6))

    def test_friday_evening_enters_on_monday(self):
        self.check("2026-08-07T09:00:00.000Z", 30e6, "open", D(2026, 8, 10))

    def test_weekend_enters_on_monday(self):
        self.check("2026-08-08T02:00:00.000Z", 30e6, "open", D(2026, 8, 10))

    def test_the_larger_tier_takes_the_smaller_haircut(self):
        self.check("2026-08-04T23:30:00.000Z", 75e6, "open", D(2026, 8, 5))


class SimulateTests(unittest.TestCase):
    PRE_OPEN = parse_feed_timestamp("2026-08-04T23:30:00.000Z")

    def test_returns_carry_both_haircuts(self):
        days = weekdays(START, 30)
        s = PriceSeries("ZZZ", payload(days, [1.0] * 30,
                                       shares=[(days[0], 30e6)]))
        sim = eventstudy.simulate(s, None, self.PRE_OPEN, 0.03)
        expected = (0.97 / 1.03 - 1.0) * 100.0
        for h in config.HORIZONS:
            self.assertAlmostEqual(sim["legs"][h]["ret"], expected)
            self.assertIsNone(sim["legs"][h]["excess"])    # no benchmark

    def test_no_bar_within_the_window_is_void_not_a_loss(self):
        late = weekdays(D(2026, 8, 20), 10)     # first bar 15 days later
        s = PriceSeries("ZZZ", payload(late, [1.0] * 10))
        sim = eventstudy.simulate(s, None, self.PRE_OPEN, 0.03)
        self.assertEqual(sim, {"void_reason": "no_entry_bar"})

        gone = PriceSeries("ZZZ", payload(weekdays(D(2026, 7, 1), 5),
                                          [1.0] * 5))
        self.assertEqual(
            eventstudy.simulate(gone, None, self.PRE_OPEN, 0.03),
            {"void_reason": "no_entry_bar"})

    def test_horizons_not_yet_reached_are_simply_absent(self):
        days = weekdays(START, 9)       # entry is index 2, so t+5 exists
        s = PriceSeries("ZZZ", payload(days, [1.0] * 9))
        sim = eventstudy.simulate(s, None, self.PRE_OPEN, 0.03)
        self.assertEqual(sorted(sim["legs"]), [0, 1, 5])

    def test_same_day_leg_only_exists_for_open_entries(self):
        days = weekdays(START, 30)
        s = PriceSeries("ZZZ", payload(days, [1.10] * 30, opens=[1.00] * 30))
        at_open = eventstudy.simulate(s, None, self.PRE_OPEN, 0.03)
        self.assertAlmostEqual(at_open["legs"][eventstudy.SAME_DAY]["ret"],
                               (1.10 * 0.97 / 1.03 - 1.0) * 100.0)
        midday = parse_feed_timestamp("2026-08-05T02:00:00.000Z")
        at_close = eventstudy.simulate(s, None, midday, 0.03)
        self.assertNotIn(eventstudy.SAME_DAY, at_close["legs"])

    def test_gap_is_measured_from_the_previous_close(self):
        days = weekdays(START, 30)
        closes = [1.0] * 30
        opens = [1.0] * 30
        opens[2] = 1.25                  # gaps up 25% on the entry morning
        s = PriceSeries("ZZZ", payload(days, closes, opens=opens))
        sim = eventstudy.simulate(s, None, self.PRE_OPEN, 0.03)
        self.assertAlmostEqual(sim["gap_pct"], 25.0)

    def test_stress_cost_floors_at_half_a_tick(self):
        # 0.7c stock: half a tick is 0.05c, 7.1% of the price
        self.assertAlmostEqual(eventstudy.stress_cost(0.03, 0.007),
                               0.0005 / 0.007)
        # 20c stock in the 3% tier: the haircut is the larger number
        self.assertAlmostEqual(eventstudy.stress_cost(0.03, 0.20), 0.03)
        # 12c stock in the 1.5% tier: half of the 0.5c tick is 2.1%
        self.assertAlmostEqual(eventstudy.stress_cost(0.015, 0.12),
                               0.0025 / 0.12)
        self.assertEqual(eventstudy.stress_cost(0.03, 0), 0.03)

    def test_stress_return_is_worse_on_a_sub_cent_stock(self):
        days = weekdays(START, 30)
        s = PriceSeries("ZZZ", payload(days, [0.007] * 30))
        sim = eventstudy.simulate(s, None, self.PRE_OPEN, 0.03)
        leg = sim["legs"][1]
        cost = 0.0005 / 0.007
        self.assertAlmostEqual(leg["stress_ret"],
                               ((1 - cost) / (1 + cost) - 1.0) * 100.0)
        self.assertLess(leg["stress_ret"], leg["ret"])

    def test_stress_uses_the_price_that_was_on_screen(self):
        """After a 1-for-20 consolidation Yahoo shows 44c for a day the
        stock actually traded at 2.2c. The tick maths needs the 2.2c."""
        days = weekdays(START, 30)
        s = PriceSeries("ZZZ", payload(days, [0.44] * 30,
                                       splits=[(days[20], 0.05)]))
        sim = eventstudy.simulate(s, None, self.PRE_OPEN, 0.03)
        self.assertAlmostEqual(sim["entry_quoted"], 0.022)
        self.assertAlmostEqual(sim["entry_price"], 0.44)

    def test_a_session_with_no_volume_is_not_fillable_under_stress(self):
        days = weekdays(START, 30)
        volumes = [1e6] * 30
        volumes[2] = 0.0
        s = PriceSeries("ZZZ", payload(days, [1.0] * 30, volumes=volumes))
        sim = eventstudy.simulate(s, None, self.PRE_OPEN, 0.03)
        self.assertFalse(sim["stress_fillable"])
        self.assertIn(1, sim["legs"])    # the book still fills it

    def test_a_leg_across_an_unadjusted_consolidation_is_suspect(self):
        days = weekdays(START, 30)
        closes = [0.02] * 10 + [0.40] * 20
        s = PriceSeries("ZZZ", payload(
            days, closes, shares=[(days[0], 4e9), (days[10], 2e8)]))
        sim = eventstudy.simulate(s, None, self.PRE_OPEN, 0.03)
        self.assertFalse(sim["legs"][1]["suspect"])
        self.assertFalse(sim["legs"][5]["suspect"])
        self.assertTrue(sim["legs"][20]["suspect"])


class StatsTests(unittest.TestCase):
    def test_tstat(self):
        xs = [1.0, 2.0, 3.0, 4.0]
        sd = math.sqrt(sum((x - 2.5) ** 2 for x in xs) / 3)
        self.assertAlmostEqual(eventstudy.tstat(xs), 2.5 / (sd / 2.0))
        self.assertIsNone(eventstudy.tstat([1.0]))
        self.assertIsNone(eventstudy.tstat([2.0, 2.0, 2.0]))

    def test_clustering_collapses_a_day_to_one_observation(self):
        """Ten identical trades on each of five days are five independent
        facts, not fifty."""
        daily = [1.0, 2.0, -1.0, 3.0, 0.5]
        pairs = [(d, x) for d, x in enumerate(daily) for _ in range(10)]
        clustered = eventstudy.clustered_tstat(pairs)
        naive = eventstudy.tstat([x for _, x in pairs])
        self.assertAlmostEqual(clustered, eventstudy.tstat(daily))
        self.assertGreater(naive, 2 * clustered)

    def test_clustering_changes_nothing_when_every_day_is_separate(self):
        xs = [1.0, -2.0, 3.5, 0.2, 4.1, -0.7]
        pairs = list(enumerate(xs))
        n = len(xs)
        # identical up to the small-sample factors: D/(D-1) vs 1/(n-1)
        self.assertAlmostEqual(eventstudy.clustered_tstat(pairs),
                               eventstudy.tstat(xs))
        self.assertIsNone(eventstudy.clustered_tstat([(0, 1.0), (0, 2.0)]))
        self.assertEqual(n, 6)

    def test_winsorised_mean_tames_one_outlier(self):
        xs = [0.0] * 39 + [400.0]
        self.assertAlmostEqual(sum(xs) / 40, 10.0)
        self.assertAlmostEqual(eventstudy.winsorised_mean(xs), 0.0)
        self.assertAlmostEqual(eventstudy.winsorised_mean([1.0, 2.0, 3.0]),
                               2.0)

    def test_gate_is_the_plan_wording(self):
        def stats(n, mean):
            return {"n": n, "mean": mean}
        self.assertEqual(eventstudy.gate(None), "THIN")
        self.assertEqual(eventstudy.gate(stats(29, 5.0)), "THIN")
        self.assertEqual(eventstudy.gate(stats(30, 0.01)), "PASS")
        self.assertEqual(eventstudy.gate(stats(30, 0.0)), "FAIL")
        self.assertEqual(eventstudy.gate(stats(500, -1.0)), "FAIL")

    def test_robust_needs_every_check(self):
        good = {"mean": 1.0, "median": 0.5, "t_cluster": 2.5, "h1": 0.4,
                "h2": 1.6, "stress_mean": 0.3}
        self.assertTrue(eventstudy.robust(good))
        for key, bad in [("median", -0.1), ("t_cluster", 1.9),
                         ("t_cluster", None), ("h1", -0.2), ("h2", None),
                         ("stress_mean", -0.01)]:
            self.assertFalse(eventstudy.robust(dict(good, **{key: bad})),
                             key)
        self.assertFalse(eventstudy.robust(None))


def fabricated_market():
    """Four companies and a benchmark over nine weeks."""
    flat = [0.10] * 45
    jump = [0.10] * 8 + [0.50] * 37          # quintuples on its news
    return {
        "AAA": payload(DAYS, jump, opens=[0.10] * 8 + [0.45] + [0.50] * 36,
                       shares=[(DAYS[0], 200e6)]),             # $20m
        "BBB": payload(DAYS, [5.0] * 45, shares=[(DAYS[0], 100e6)]),  # $500m
        "A1M": payload(DAYS, flat, shares=[(DAYS[0], 600e6)]),  # $60m
        "DDD": payload(DAYS, flat),                      # no share history
        "^AXSO": BENCH,
    }


def fabricated_corpus():
    day9 = DAYS[8].isoformat()               # Thursday 13 Aug 2026
    pre_open = f"{DAYS[7].isoformat()}T23:30:00.000Z"    # 09:30 that Thursday
    midday = f"{day9}T02:00:00.000Z"
    return [
        corpus_row("2924-00000001-A", "AAA", "Significant Drilling Results",
                   pre_open, ["Progress Report"]),
        # a second document the same morning: one decision, not two
        corpus_row("2924-00000002-A", "AAA", "High-Grade Assay Results",
                   f"{DAYS[7].isoformat()}T23:40:00.000Z",
                   ["Progress Report"]),
        corpus_row("2924-00000003-A", "BBB", "Contract Award with Utility",
                   pre_open, ["Progress Report"]),
        corpus_row("2924-00000004-A", "A1M", "Contract Award with Utility",
                   midday, ["Progress Report"]),
        corpus_row("2924-00000005-A", "DDD", "Maiden Resource Estimate",
                   pre_open, ["Progress Report"]),
        corpus_row("2924-00000006-A", "CCC", "Significant Drilling Results",
                   pre_open, ["Progress Report"]),       # no prices at all
        corpus_row("2924-00000007-A", "AAA", "Trading Halt",
                   midday, ["Trading Halt"]),
        corpus_row("2924-00000008-A", "A1M", "Strategic Review Update",
                   pre_open, ["Company Administration - Other"]),
    ]


class BuildEventsTests(unittest.TestCase):
    def setUp(self):
        market = fabricated_market()
        self.bench = PriceSeries("^AXSO", market.pop("^AXSO"))
        self.prices = {t: PriceSeries(t, p) for t, p in market.items()}
        self.prices["CCC"] = None
        self.events = {e["doc_key"][5:13]: e for e in eventstudy.build_events(
            fabricated_corpus(), self.prices, self.bench)}

    def test_filter_outcomes(self):
        got = {k: (e["passed"], e["reason"], e["category"])
               for k, e in self.events.items()}
        self.assertEqual(got, {
            "00000001": (True, "ok", "DRILLING_RESULTS"),
            "00000002": (True, "ok", "DRILLING_RESULTS"),
            "00000003": (False, "mcap_above_ceiling", "CONTRACT_WIN"),
            "00000004": (True, "ok", "CONTRACT_WIN"),
            "00000005": (False, "mcap_unknown", "RESOURCE_UPGRADE"),
            "00000006": (False, "mcap_unknown", "DRILLING_RESULTS"),
            "00000007": (False, "headline_excluded", None),
            "00000008": (False, "no_category_match", None),
        })

    def test_size_screen_does_not_see_the_announcement_day(self):
        """AAA is $20m the night before and $100m after its news. A screen
        that peeked would throw out the very trade it is looking for."""
        e = self.events["00000001"]
        self.assertAlmostEqual(e["mcap"], 20e6)
        self.assertAlmostEqual(e["sim"]["haircut"], 0.03)
        self.assertAlmostEqual(e["sim"]["gap_pct"], 350.0)

    def test_codes_with_digits_are_traded(self):
        e = self.events["00000004"]
        self.assertTrue(e["passed"])
        self.assertAlmostEqual(e["sim"]["haircut"], 0.015)   # $60m tier
        self.assertEqual(e["sim"]["entry_field"], "close")

    def test_rejected_but_in_universe_events_are_still_simulated(self):
        """The by-native-type view needs returns for what the filter turned
        away, or it cannot show what the filter is missing."""
        self.assertIsNotNone(self.events["00000008"]["sim"])
        self.assertIsNotNone(self.events["00000007"]["sim"])
        self.assertIsNone(self.events["00000003"]["sim"])    # too big
        self.assertIsNone(self.events["00000006"]["sim"])    # no prices

    def test_same_morning_documents_merge_into_one_trade(self):
        passed = [e for e in self.events.values() if e["passed"]]
        merged = eventstudy.dedupe(
            passed, lambda e: (e["ticker"], e["sim"]["entry_day"],
                               e["category"]))
        self.assertEqual([e["doc_key"][5:13] for e in merged],
                         ["00000001", "00000004"])

    def test_report_builds_and_counts_add_up(self):
        events = list(self.events.values())
        meta = {"start": DAYS[0], "end": DAYS[-1], "benchmark": "^AXSO"}
        text, traded = eventstudy.build_report(events, meta)
        self.assertEqual(len(traded), 2)
        self.assertIn("Evaluated 8, passed 3", text)
        self.assertIn("mcap_unknown", text)
        self.assertIn("0.2.6 GATE", text)
        for section in ("READOUT", "EX-EARNINGS", "BY USUAL LIQUIDITY",
                        "ALL TRADEABLE", "BE%"):
            self.assertIn(section, text)
        self.assertIn("3 documents -> 2 after merging", text)
        self.assertIn("codes containing a digit: 1", text)
        # nothing reaches N of 30 on eight announcements
        self.assertIn("Cells passing the plan's gate: 0 of", text)

        _, per_doc = eventstudy.build_report(events, meta,
                                             dedupe_events=False)
        self.assertEqual(len(per_doc), 3)


class CellTests(unittest.TestCase):
    def events(self, excess_by_day):
        out = []
        for n, (day, excess, fillable) in enumerate(excess_by_day):
            out.append({"sim": {
                "void_reason": None, "entry_day": day, "haircut": 0.03,
                "stress_fillable": fillable,
                # benchmark +1.0, so ret = excess + 1; gross is 5 above ret
                "legs": {1: {"ret": excess + 1.0, "excess": excess,
                             "bench_ret": 1.0, "gross": excess + 6.0,
                             "gross_excess": excess + 5.0,
                             "stress_ret": excess, "stress_excess": excess - 1.0,
                             "suspect": False, "exit_day": day}}}})
        return out

    def test_cell_statistics(self):
        evs = self.events([(D(2026, 8, 3), 4.0, True),
                           (D(2026, 8, 4), -2.0, True),
                           (D(2026, 9, 1), 1.0, False),
                           (D(2026, 9, 2), 5.0, True)])
        s = eventstudy.cell(evs, 1, midpoint=D(2026, 8, 20))
        self.assertEqual(s["n"], 4)
        self.assertAlmostEqual(s["mean"], 2.0)
        self.assertAlmostEqual(s["median"], 2.5)
        self.assertAlmostEqual(s["mean_ret"], 3.0)
        self.assertAlmostEqual(s["win"], 100.0 * 3 / 4)   # net return > 0
        self.assertAlmostEqual(s["h1"], 1.0)
        self.assertAlmostEqual(s["h2"], 3.0)
        # gross is what the price did before costs, net of the benchmark
        self.assertAlmostEqual(s["gross"], 2.0 + 5.0)
        self.assertAlmostEqual(s["gross_median"], 2.5 + 5.0)
        self.assertAlmostEqual(s["cost"], 0.03)
        # t(g) is the clustered t of the gross excess series itself
        self.assertAlmostEqual(
            s["t_gross"], eventstudy.clustered_tstat(
                [(D(2026, 8, 3), 9.0), (D(2026, 8, 4), 3.0),
                 (D(2026, 9, 1), 6.0), (D(2026, 9, 2), 10.0)]))
        # the unfillable entry drops out of the stress test only
        self.assertEqual(s["stress_n"], 3)
        self.assertAlmostEqual(s["stress_mean"], (3.0 - 3.0 + 4.0) / 3)

    def test_void_and_suspect_legs_are_left_out(self):
        evs = self.events([(D(2026, 8, 3), 4.0, True),
                           (D(2026, 8, 4), 900.0, True)])
        evs[1]["sim"]["legs"][1]["suspect"] = True
        evs.append({"sim": {"void_reason": "no_entry_bar"}})
        evs.append({"sim": None})
        s = eventstudy.cell(evs, 1)
        self.assertEqual(s["n"], 1)
        self.assertAlmostEqual(s["mean"], 4.0)
        self.assertIsNone(eventstudy.cell(evs, 20))


class BreakEvenTests(unittest.TestCase):
    """BE is the uniform per-side cost that brings mean excess to zero."""

    def events_with_gross(self, gross_pct, bench_pct=0.0, n=6):
        evs = []
        for k in range(n):
            ret = 0.0   # unused by the break-even arithmetic
            evs.append({"sim": {
                "void_reason": None, "entry_day": D(2026, 8, 3 + k),
                "haircut": 0.03, "stress_fillable": True,
                "legs": {1: {"ret": ret, "excess": ret - bench_pct,
                             "bench_ret": bench_pct, "gross": gross_pct,
                             "gross_excess": gross_pct - bench_pct,
                             "stress_ret": ret, "stress_excess": ret,
                             "suspect": False, "exit_day": D(2026, 8, 4)}}}})
        return evs

    def net_excess_at(self, gross_pct, bench_pct, h):
        net = ((1 + gross_pct / 100.0) * (1 - h) / (1 + h) - 1.0) * 100.0
        return net - bench_pct

    def test_break_even_is_the_cost_that_zeroes_the_excess(self):
        for gross, bench in [(10.0, 0.0), (4.0, 1.5), (25.0, -2.0),
                             (0.5, 0.0)]:
            be = eventstudy.cell(self.events_with_gross(gross, bench), 1)["be"]
            self.assertGreater(be, 0)
            self.assertAlmostEqual(self.net_excess_at(gross, bench, be), 0.0,
                                   places=9, msg=f"gross {gross} bench {bench}")

    def test_a_ten_percent_move_survives_about_4_8_percent_per_side(self):
        be = eventstudy.cell(self.events_with_gross(10.0), 1)["be"]
        self.assertAlmostEqual(be, 0.047619, places=5)

    def test_a_move_that_does_not_beat_the_benchmark_has_no_break_even(self):
        for gross, bench in [(0.0, 0.0), (-3.0, 0.0), (1.0, 2.0)]:
            be = eventstudy.cell(self.events_with_gross(gross, bench), 1)["be"]
            self.assertLessEqual(be, 0, msg=f"gross {gross} bench {bench}")

    def test_text(self):
        self.assertEqual(eventstudy._be_text(0.0476).strip(), "4.8")
        self.assertEqual(eventstudy._be_text(0.0).strip(), "<0")
        self.assertEqual(eventstudy._be_text(-0.02).strip(), "<0")
        self.assertEqual(eventstudy._be_text(None).strip(), "n/a")


class GrossAndLiquidityTests(unittest.TestCase):
    PRE_OPEN = parse_feed_timestamp("2026-08-04T23:30:00.000Z")

    def test_gross_is_the_move_before_any_haircut(self):
        days = weekdays(START, 30)
        closes = [1.0] * 30
        closes[3] = 1.20                 # entry is index 2, so t+1 is +20%
        s = PriceSeries("ZZZ", payload(days, closes))
        bench = PriceSeries("^AXSO", payload(days, [100.0] * 2 + [100.0, 105.0]
                                             + [100.0] * 26))
        sim = eventstudy.simulate(s, bench, self.PRE_OPEN, 0.03)
        leg = sim["legs"][1]
        self.assertAlmostEqual(leg["gross"], 20.0)
        self.assertAlmostEqual(leg["bench_ret"], 5.0)
        self.assertAlmostEqual(leg["gross_excess"], 15.0)
        self.assertAlmostEqual(leg["ret"], (1.2 * 0.97 / 1.03 - 1) * 100)
        self.assertAlmostEqual(sim["legs"][eventstudy.SAME_DAY]["gross"], 0.0)

    def test_turnover_is_adjusted_close_times_adjusted_volume(self):
        """Yahoo restates volume for splits (98 of 104 recorded splits in the
        real data). Multiplying by the price that was actually quoted would
        be wrong by the split ratio."""
        days = weekdays(START, 30)
        # 1-for-20 consolidation on session 20: price and volume both shown
        # restated, so turnover is the same $176,000 a day throughout
        s = PriceSeries("ZZZ", payload(
            days, [0.44] * 30, volumes=[400_000.0] * 30,
            splits=[(days[20], 0.05)]))
        self.assertAlmostEqual(s.median_turnover_before(days[15]), 176_000.0)
        self.assertAlmostEqual(s.median_turnover_before(days[28]), 176_000.0)
        sim = eventstudy.simulate(s, None, self.PRE_OPEN, 0.03)
        self.assertAlmostEqual(sim["entry_turnover"], 176_000.0)

    def test_prior_turnover_never_includes_the_entry_session(self):
        days = weekdays(START, 40)
        # Pre-open on Tuesday 1 Sep (Sydney): entry is that morning's open,
        # the 22nd session, with 21 sessions of history before it.
        published = parse_feed_timestamp("2026-08-31T23:30:00.000Z")
        entry = days.index(D(2026, 9, 1))
        volumes = [10_000.0] * 40
        volumes[entry] = 50_000_000.0    # the announcement's own volume
        s = PriceSeries("ZZZ", payload(days, [1.0] * 40, volumes=volumes))
        sim = eventstudy.simulate(s, None, published, 0.03)
        self.assertEqual(sim["entry_day"], days[entry])
        self.assertAlmostEqual(sim["prior_turnover"], 10_000.0)
        self.assertAlmostEqual(sim["entry_turnover"], 50_000_000.0)

    def test_too_little_history_is_unknown(self):
        days = weekdays(START, 10)
        s = PriceSeries("ZZZ", payload(days, [1.0] * 10))
        self.assertIsNone(s.median_turnover_before(days[3]))
        self.assertIsNotNone(s.median_turnover_before(days[8]))

    def test_zero_volume_sessions_count_as_zero(self):
        days = weekdays(START, 30)
        volumes = [0.0] * 14 + [1_000_000.0] * 16
        s = PriceSeries("ZZZ", payload(days, [1.0] * 30, volumes=volumes))
        self.assertEqual(s.median_turnover_before(days[20]), 0.0)

    def test_liquidity_bands(self):
        self.assertEqual(eventstudy._liq_band(None), "unknown")
        self.assertEqual(eventstudy._liq_band(0.0), "under $10k a day")
        self.assertEqual(eventstudy._liq_band(9_999), "under $10k a day")
        self.assertEqual(eventstudy._liq_band(10_000), "$10k - $50k")
        self.assertEqual(eventstudy._liq_band(300_000), "$250k - $1m")
        self.assertEqual(eventstudy._liq_band(5e9), "over $1m a day")



def pre_open_at(day):
    """08:00 Sydney on `day`, as a UTC datetime. Built from local time on
    purpose: a fixed UTC string is 09:30 in winter and 10:30 under daylight
    saving, which silently turns a pre-open trade into a mid-session one."""
    return dt.datetime.combine(day, dt.time(8, 0),
                               tzinfo=classify.__globals__["SYDNEY"]
                               ).astimezone(dt.timezone.utc)


class PlaceboTests(unittest.TestCase):
    """The control: same stock, random mornings."""

    def setUp(self):
        self.days = weekdays(START, 160)
        self.start, self.end = self.days[0], self.days[-1]
        self.mid = 80

    def real_event(self, ticker, day, series):
        sim = eventstudy.simulate(series, None, pre_open_at(day), 0.03)
        return {"doc_key": f"{ticker}-{day}", "ticker": ticker,
                "syd_date": day, "market_state": "PRE_OPEN", "sim": sim,
                "mcap": 20e6}

    def stock(self, **kw):
        return PriceSeries("ZZZ", payload(self.days, [1.0] * 160,
                                          shares=[(self.days[0], 20e6)], **kw))

    def test_placebos_stay_away_from_the_real_announcement(self):
        series = self.stock()
        real = self.real_event("ZZZ", self.days[self.mid], series)
        out = eventstudy.placebo_trades([real], {"ZZZ": series}, None,
                                        self.start, self.end, draws=25, seed=1)
        placebos = out[real["doc_key"]]
        self.assertEqual(len(placebos), 25)
        keep = dt.timedelta(days=config.STUDY_PLACEBO_EXCLUDE_DAYS)
        reach = dt.timedelta(days=config.STUDY_PLACEBO_WINDOW_DAYS)
        for p in placebos:
            gap = abs(p["syd_date"] - real["syd_date"])
            self.assertGreater(gap, keep)       # clear of the real reaction
            self.assertLessEqual(gap, reach)    # but from the same season
            self.assertEqual(p["sim"]["entry_field"], "open")

    def test_placebo_holding_windows_never_touch_the_real_reaction(self):
        """A placebo entered 10 days before the announcement would hold
        straight through it. The exclusion gap is longer than the longest
        horizon so that cannot happen."""
        series = self.stock()
        real = self.real_event("ZZZ", self.days[self.mid], series)
        out = eventstudy.placebo_trades([real], {"ZZZ": series}, None,
                                        self.start, self.end, draws=30, seed=9)
        longest = series.dates[series.index_on_or_after(real["syd_date"])]
        for p in out[real["doc_key"]]:
            exit_day = max(leg["exit_day"] for leg in p["sim"]["legs"].values())
            before = p["syd_date"] < real["syd_date"]
            if before:
                self.assertLess(exit_day, longest)

    def test_placebos_obey_the_size_screen(self):
        big = PriceSeries("BIG", payload(self.days, [5.0] * 160,
                                         shares=[(self.days[0], 500e6)]))
        real = self.real_event("BIG", self.days[self.mid], big)
        out = eventstudy.placebo_trades([real], {"BIG": big}, None,
                                        self.start, self.end, draws=5, seed=1)
        self.assertEqual(out, {})

    def test_same_seed_same_placebos(self):
        series = self.stock()
        real = self.real_event("ZZZ", self.days[self.mid], series)
        a = eventstudy.placebo_trades([real], {"ZZZ": series}, None,
                                      self.start, self.end, draws=4, seed=3)
        b = eventstudy.placebo_trades([real], {"ZZZ": series}, None,
                                      self.start, self.end, draws=4, seed=3)
        self.assertEqual([p["syd_date"] for p in a[real["doc_key"]]],
                         [p["syd_date"] for p in b[real["doc_key"]]])

    def test_effect_is_the_paired_difference(self):
        """The stock steps up 10% at the open on day 30 and stays there, so
        a trade entered at that open gains 10% and any other morning gains
        nothing. The announcement effect is therefore +10 points."""
        closes = [1.0] * self.mid + [1.1] * (160 - self.mid)
        opens = list(closes)
        opens[self.mid] = 1.0
        series = PriceSeries("ZZZ", payload(
            self.days, closes, opens=opens, shares=[(self.days[0], 20e6)]))
        bench = PriceSeries("^AXSO", payload(self.days, [100.0] * 160))
        events = [self.real_event("ZZZ", self.days[self.mid], series)]
        for e in events:       # rebuild with the benchmark so excess exists
            e["sim"] = eventstudy.simulate(
                series, bench, pre_open_at(self.days[self.mid]), 0.03)
        placebo = eventstudy.placebo_trades(events, {"ZZZ": series}, bench,
                                            self.start, self.end, draws=6,
                                            seed=2)
        n, mean, t = eventstudy.announcement_effect(events, placebo, 1)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(mean, 10.0)
        self.assertIsNone(t)        # one paired observation has no spread

    def test_no_effect_when_nothing_special_happens(self):
        series = self.stock()
        bench = PriceSeries("^AXSO", payload(self.days, [100.0] * 160))
        real = self.real_event("ZZZ", self.days[self.mid], series)
        real["sim"] = eventstudy.simulate(
            series, bench, pre_open_at(self.days[self.mid]), 0.03)
        placebo = eventstudy.placebo_trades([real], {"ZZZ": series}, bench,
                                            self.start, self.end, draws=4,
                                            seed=5)
        n, mean, _ = eventstudy.announcement_effect([real], placebo, 5)
        self.assertAlmostEqual(mean, 0.0)

    def test_unpriced_tickers_are_skipped(self):
        series = self.stock()
        real = self.real_event("ZZZ", self.days[self.mid], series)
        self.assertEqual(
            eventstudy.placebo_trades([real], {"ZZZ": None}, None, self.start,
                                      self.end, draws=3, seed=1), {})
        self.assertIsNone(eventstudy.announcement_effect([real], {}, 1))


class RunEndToEndTests(unittest.TestCase):
    def setUp(self):
        fresh_research_db()
        rows = fabricated_corpus()
        histstore.upsert_announcements(rows)
        # A real harvest always records the days it covered, and the study
        # takes its date range from them.
        histstore.mark_days({r["syd_date"]: 1 for r in rows}, "ps")
        self.market = fabricated_market()
        self.fetched = []

    def fake_fetch(self, ticker, start, end, attempts=4):
        self.fetched.append(ticker)
        if ticker in self.market:
            return "ok", self.market[ticker]
        return "no_data", None

    def test_run_writes_report_and_trades_and_reuses_the_cache(self):
        with mock.patch.object(histprices, "_fetch_one", self.fake_fetch):
            text, traded = eventstudy.run(progress=lambda m: None)
            self.assertEqual(len(traded), 2)
            self.assertIn("benchmark: ^AXSO", text)
            # only codes that look like ordinary shares are priced
            self.assertEqual(sorted(set(self.fetched)),
                             ["A1M", "AAA", "BBB", "CCC", "DDD", "^AXSO"])

            report = os.path.join(config.RESEARCH_DIR,
                                  "eventstudy_report.txt")
            trades = os.path.join(config.RESEARCH_DIR,
                                  "eventstudy_trades.csv")
            with open(report, encoding="utf-8") as fh:
                self.assertEqual(fh.read().strip(), text.strip())
            with open(trades, encoding="utf-8") as fh:
                lines = fh.read().strip().splitlines()
            self.assertEqual(len(lines), 3)              # header + 2 trades
            self.assertTrue(lines[0].startswith("doc_key,ticker,"))

            self.fetched.clear()
            eventstudy.run(progress=lambda m: None)
            self.assertEqual(self.fetched, [])

    def test_an_empty_corpus_says_what_to_run(self):
        fresh_research_db()
        with self.assertRaises(SystemExit) as ctx:
            eventstudy.run(progress=lambda m: None)
        self.assertIn("openclaw.harvest", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
