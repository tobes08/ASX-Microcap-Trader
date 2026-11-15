import datetime as dt
import unittest
from unittest import mock

from openclaw import config, harvest, histstore

from .helpers import fresh_research_db

D = dt.date


def item(n, when, price_sensitive=True, symbol="ABC"):
    return {"documentKey": f"2924-{n:08d}-6A{n}", "symbol": symbol,
            "headline": f"Announcement {n}", "date": when,
            "isPriceSensitive": price_sensitive,
            "announcementTypes": ["Progress Report"]}


class FakeFeed:
    """Stands in for feed.fetch_query: serves a fixed set of items, honours
    the date window and the price-sensitive filter, pages, and caps the
    reported count the way the real feed does."""

    def __init__(self, items, honour_dates=True, honour_ps=True,
                 lie_about_count=0, fail_on_page=None):
        self.items = items
        self.honour_dates = honour_dates
        self.honour_ps = honour_ps
        self.lie_about_count = lie_about_count
        self.fail_on_page = fail_on_page
        self.calls = []

    def __call__(self, params, max_attempts=None):
        self.calls.append(dict(params))
        if self.fail_on_page is not None and params["page"] == self.fail_on_page:
            return None
        rows = []
        for it in self.items:
            syd = harvest.to_row(it)["syd_date"]
            if self.honour_dates and not (
                    params["dateStart"] <= syd <= params["dateEnd"]):
                continue
            if (self.honour_ps and params.get("priceSensitiveOnly") == "true"
                    and not it["isPriceSensitive"]):
                continue
            rows.append(it)
        rows.sort(key=lambda it: it["date"], reverse=True)
        count = min(len(rows), config.FEED_MAX_RESULT_WINDOW)
        per = params["itemsPerPage"]
        page = rows[params["page"] * per:(params["page"] + 1) * per]
        if params["page"] * per >= config.FEED_MAX_RESULT_WINDOW:
            return {"items": [], "count": 0}
        return {"items": page, "count": count + self.lie_about_count}


class PlanWindowsTests(unittest.TestCase):
    def test_cuts_into_windows_newest_first(self):
        w = harvest.plan_windows(D(2026, 1, 1), D(2026, 1, 10), set(), 4)
        self.assertEqual(w, [(D(2026, 1, 9), D(2026, 1, 10)),
                             (D(2026, 1, 5), D(2026, 1, 8)),
                             (D(2026, 1, 1), D(2026, 1, 4))])

    def test_skips_days_already_harvested(self):
        done = {"2026-01-03", "2026-01-04"}
        w = harvest.plan_windows(D(2026, 1, 1), D(2026, 1, 6), done, 10)
        self.assertEqual(w, [(D(2026, 1, 5), D(2026, 1, 6)),
                             (D(2026, 1, 1), D(2026, 1, 2))])

    def test_nothing_to_do(self):
        done = {"2026-01-01", "2026-01-02"}
        self.assertEqual(
            harvest.plan_windows(D(2026, 1, 1), D(2026, 1, 2), done, 7), [])


class ToRowTests(unittest.TestCase):
    def test_sydney_date_and_state_come_from_the_utc_stamp(self):
        # 22:55 UTC on the 12th is 09:55 on Friday the 13th in Sydney (AEDT).
        row = harvest.to_row(item(1, "2026-11-12T22:55:16.000Z"))
        self.assertEqual(row["syd_date"], "2026-11-13")
        self.assertEqual(row["market_state"], "PRE_OPEN")
        self.assertEqual(row["published_utc"], "2026-11-12T22:55:16+00:00")
        self.assertEqual(row["ann_id"], "00000001")
        self.assertIsNone(row["pair_suffix"])

    def test_paired_keys_share_an_announcement_id(self):
        ps = harvest.to_row({"documentKey": "2924-03100606_PS-6A1329605",
                             "symbol": "GSS", "headline": "x",
                             "date": "2026-11-13T06:29:27.000Z",
                             "isPriceSensitive": True})
        nps = harvest.to_row({"documentKey": "2924-03100606_NPS-3A695387",
                              "symbol": "TOV", "headline": "x",
                              "date": "2026-11-13T06:29:27.000Z",
                              "isPriceSensitive": False})
        self.assertEqual((ps["ann_id"], ps["pair_suffix"]), ("03100606", "PS"))
        self.assertEqual((nps["ann_id"], nps["pair_suffix"]),
                         ("03100606", "NPS"))

    def test_unusable_items_are_dropped(self):
        self.assertIsNone(harvest.to_row({"symbol": "ABC", "date": "x"}))
        self.assertIsNone(harvest.to_row({"documentKey": "k", "date": "junk"}))


class HarvestWindowTests(unittest.TestCase):
    def setUp(self):
        fresh_research_db()
        self.per_page = mock.patch.object(config, "HARVEST_ITEMS_PER_PAGE", 3)
        self.per_page.start()
        self.addCleanup(self.per_page.stop)

    def harvest(self, fake, start, end, scope="all"):
        with mock.patch.object(harvest.feed, "fetch_query", fake):
            return harvest.harvest_window(start, end, scope)

    def stored(self):
        return [r["doc_key"] for r in histstore.connect().execute(
            "SELECT doc_key FROM hist_announcements ORDER BY doc_key")]

    def days(self):
        return {r["syd_date"]: (r["scope"], r["items"])
                for r in histstore.connect().execute(
                    "SELECT * FROM harvest_days")}

    def test_pages_through_a_window_and_marks_every_day(self):
        items = [item(n, f"2026-10-0{1 + n % 2}T01:00:0{n}.000Z")
                 for n in range(8)]
        fake = FakeFeed(items)
        n = self.harvest(fake, D(2026, 10, 1), D(2026, 10, 3))
        self.assertEqual(n, 8)
        self.assertEqual(len(self.stored()), 8)
        self.assertEqual([c["page"] for c in fake.calls], [0, 1, 2])
        # the empty third day is recorded too, so it is never refetched
        self.assertEqual(self.days(), {"2026-10-01": ("all", 4),
                                       "2026-10-02": ("all", 4),
                                       "2026-10-03": ("all", 0)})

    def test_a_failed_page_marks_nothing(self):
        items = [item(n, f"2026-10-01T01:00:0{n}.000Z") for n in range(8)]
        n = self.harvest(FakeFeed(items, fail_on_page=1),
                         D(2026, 10, 1), D(2026, 10, 1))
        self.assertIsNone(n)
        self.assertEqual(self.days(), {})

    def test_a_short_window_is_left_for_a_rerun(self):
        items = [item(n, f"2026-10-01T01:00:0{n}.000Z") for n in range(4)]
        n = self.harvest(FakeFeed(items, lie_about_count=2),
                         D(2026, 10, 1), D(2026, 10, 1))
        self.assertIsNone(n)
        self.assertEqual(self.days(), {})

    def test_a_window_at_the_cap_is_split(self):
        items = [item(n, f"2026-10-0{1 + n % 4}T01:00:0{n % 10}.000Z")
                 for n in range(12)]
        fake = FakeFeed(items)
        with mock.patch.object(config, "FEED_MAX_RESULT_WINDOW", 10):
            n = self.harvest(fake, D(2026, 10, 1), D(2026, 10, 4))
        self.assertEqual(n, 12)
        self.assertEqual(len(self.stored()), 12)
        self.assertEqual(len(self.days()), 4)
        windows = {(c["dateStart"], c["dateEnd"]) for c in fake.calls}
        self.assertIn(("2026-10-01", "2026-10-02"), windows)
        self.assertIn(("2026-10-03", "2026-10-04"), windows)

    def test_an_ignored_date_filter_is_refused(self):
        """The feed silently ignores parameters it does not know. If the
        date filter stops working it returns the live feed, which must not
        be filed as history."""
        items = [item(n, f"2026-11-1{n}T01:00:00.000Z") for n in range(3)]
        n = self.harvest(FakeFeed(items, honour_dates=False),
                         D(2025, 3, 3), D(2025, 3, 5))
        self.assertIsNone(n)
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.days(), {})

    def test_an_ignored_price_sensitive_filter_is_refused(self):
        items = [item(1, "2026-10-01T01:00:00.000Z", price_sensitive=True),
                 item(2, "2026-10-01T02:00:00.000Z", price_sensitive=False)]
        n = self.harvest(FakeFeed(items, honour_ps=False),
                         D(2026, 10, 1), D(2026, 10, 1), scope="ps")
        self.assertIsNone(n)
        self.assertEqual(self.days(), {})

    def test_price_sensitive_scope_sends_the_filter(self):
        items = [item(1, "2026-10-01T01:00:00.000Z", price_sensitive=True),
                 item(2, "2026-10-01T02:00:00.000Z", price_sensitive=False)]
        fake = FakeFeed(items)
        n = self.harvest(fake, D(2026, 10, 1), D(2026, 10, 1), scope="ps")
        self.assertEqual(n, 1)
        self.assertEqual(fake.calls[0]["priceSensitiveOnly"], "true")
        self.assertEqual(self.days(), {"2026-10-01": ("ps", 1)})


class RunTests(unittest.TestCase):
    def setUp(self):
        fresh_research_db()
        self.items = [item(n, f"2026-10-0{1 + n % 3}T01:00:0{n}.000Z",
                           price_sensitive=bool(n % 2)) for n in range(9)]

    def run_harvest(self, scope, fake=None):
        fake = fake or FakeFeed(self.items)
        with mock.patch.object(harvest.feed, "fetch_query", fake):
            summary = harvest.run(D(2026, 10, 1), D(2026, 10, 3), scope,
                                  progress=lambda m: None)
        return summary, fake

    def test_rerun_fetches_nothing(self):
        first, _ = self.run_harvest("all")
        self.assertEqual(first["stored"], 9)
        second, fake = self.run_harvest("all")
        self.assertEqual((second["windows"], second["stored"]), (0, 0))
        self.assertEqual(fake.calls, [])

    def test_full_harvest_upgrades_a_price_sensitive_one(self):
        self.run_harvest("ps")
        self.assertEqual(histstore.coverage()["announcements"], 4)
        summary, _ = self.run_harvest("all")
        self.assertEqual(summary["stored"], 9)
        c = histstore.coverage()
        self.assertEqual((c["announcements"], c["full_days"], c["ps_days"]),
                         (9, 3, 0))

    def test_price_sensitive_harvest_never_downgrades_a_full_one(self):
        self.run_harvest("all")
        summary, fake = self.run_harvest("ps")
        self.assertEqual(fake.calls, [])
        self.assertEqual(histstore.coverage()["full_days"], 3)

    def test_today_is_never_recorded_as_complete(self):
        today = dt.datetime.now(harvest.SYDNEY).date()
        fake = FakeFeed([])
        with mock.patch.object(harvest.feed, "fetch_query", fake):
            harvest.run(today - dt.timedelta(days=1), today, "all",
                        progress=lambda m: None)
        self.assertNotIn(today.isoformat(), histstore.harvested_days("all"))
        self.assertIn((today - dt.timedelta(days=1)).isoformat(),
                      histstore.harvested_days("all"))

    def test_failed_windows_are_counted_and_retried_next_run(self):
        summary, _ = self.run_harvest("all", FakeFeed(self.items,
                                                      fail_on_page=0))
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(histstore.coverage()["days"], 0)
        summary, _ = self.run_harvest("all")
        self.assertEqual((summary["failed"], summary["stored"]), (0, 9))


class SplitDocKeyTests(unittest.TestCase):
    def test_shapes(self):
        f = histstore.split_doc_key
        self.assertEqual(f("2924-03097959-6A1328564"), ("03097959", None))
        self.assertEqual(f("2924-03100606_PS-6A1329605"), ("03100606", "PS"))
        self.assertEqual(f("2924-03100606_NPS-3A695387"), ("03100606", "NPS"))
        self.assertEqual(f("SELFTEST-1"), (None, None))
        self.assertEqual(f(None), (None, None))


if __name__ == "__main__":
    unittest.main()
