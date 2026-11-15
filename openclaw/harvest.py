"""
Sprint 0.2.1: historical harvest.

Plain paging of the announcements feed stops at 10,000 items, which is about
three and a half weeks. The feed also takes inclusive Sydney-date windows,
and those reach back years, so history is harvested one window at a time
into the research database.

    python -m openclaw.harvest                  # last 12 months, everything
    python -m openclaw.harvest --months 24 --price-sensitive-only
    python -m openclaw.harvest --start 2025-07-01 --end 2025-12-31
    python -m openclaw.harvest --status

Resumable: completed days are recorded, a rerun only fetches what is
missing, and a window that fails leaves nothing half-marked. Deliberately
slow (see config.HARVEST_MIN_INTERVAL). The endpoint is unofficial, a year
of history is about a hundred requests, and there is no prize for speed.

The corpus is for private, personal research. It lives under data/research,
which is gitignored. Do not commit it or pass it on.
"""
import argparse
import datetime as _dt
import json
import sys
import time
from collections import Counter

from . import config, log as oclog

# Before anything imports the feed module: keep research logging out of the
# live poller's rotating log file.
oclog.setup(filename="research.log")

from . import feed, histstore  # noqa: E402
from .marketstate import (SYDNEY, classify, parse_feed_timestamp,  # noqa: E402
                          to_sydney)

log = oclog.get("harvest")

ONE_DAY = _dt.timedelta(days=1)
_last_request = 0.0


def _throttle():
    global _last_request
    wait = config.HARVEST_MIN_INTERVAL - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()


def _fetch(params):
    _throttle()
    return feed.fetch_query(params)


def to_row(item, now=None):
    """Map a raw feed item onto a corpus row. Returns None for an item with
    no usable key or timestamp."""
    rec = feed.normalise(item)
    doc_key = rec.get("doc_key")
    published = parse_feed_timestamp(rec.get("published_raw"))
    if not doc_key or published is None:
        return None
    ann_id, suffix = histstore.split_doc_key(doc_key)
    return {
        "doc_key": doc_key,
        "ann_id": ann_id,
        "pair_suffix": suffix,
        "ticker": rec["ticker"],
        "headline": rec["headline"],
        "published_utc": published.isoformat(),
        "syd_date": to_sydney(published).date().isoformat(),
        "market_state": classify(published),
        "price_sensitive": int(bool(rec["price_sensitive"])),
        "native_types": json.dumps(item.get("announcementTypes") or []),
        "raw_json": json.dumps(item, default=str, ensure_ascii=False,
                               separators=(",", ":")),
        "harvested_utc": now or histstore.utcnow(),
    }


def plan_windows(start, end, done, window_days):
    """Cut the not-yet-harvested days in [start, end] into windows of at
    most window_days consecutive days. Newest window first, so an
    interrupted run has the most recent history."""
    windows = []
    run_start = None
    day = start
    while day <= end:
        if day.isoformat() not in done:
            if run_start is None:
                run_start = day
            if (day - run_start).days + 1 >= window_days:
                windows.append((run_start, day))
                run_start = None
        elif run_start is not None:
            windows.append((run_start, day - ONE_DAY))
            run_start = None
        day += ONE_DAY
    if run_start is not None:
        windows.append((run_start, end))
    windows.reverse()
    return windows


def harvest_window(start, end, scope):
    """Fetch and store everything published on Sydney dates start..end.

    Returns the number of announcements stored, or None if the window could
    not be completed, in which case no day is marked as done.
    """
    per_page = config.HARVEST_ITEMS_PER_PAGE
    base = {"itemsPerPage": per_page, "dateStart": start.isoformat(),
            "dateEnd": end.isoformat()}
    if scope == "ps":
        base["priceSensitiveOnly"] = "true"

    data = _fetch(dict(base, page=0))
    if data is None:
        return None
    count = data.get("count") or 0

    if count >= config.FEED_MAX_RESULT_WINDOW:
        if start == end:
            log.error("%s alone reaches the feed's %s item cap; cannot split "
                      "a single day any further", start,
                      config.FEED_MAX_RESULT_WINDOW)
            return None
        mid = start + (end - start) // 2
        log.info("%s..%s hits the item cap, splitting", start, end)
        newer = harvest_window(mid + ONE_DAY, end, scope)
        older = harvest_window(start, mid, scope)
        if newer is None or older is None:
            return None
        return newer + older

    items = list(data["items"])
    page = 0
    last_len = len(data["items"])
    while len(items) < count and last_len == per_page:
        page += 1
        data = _fetch(dict(base, page=page))
        if data is None:
            return None
        last_len = len(data["items"])
        items.extend(data["items"])

    if len(items) < count:
        log.warning("%s..%s: feed reported %s items but served %s, leaving "
                    "the window for a rerun", start, end, count, len(items))
        return None

    now = histstore.utcnow()
    rows = [r for r in (to_row(i, now) for i in items) if r]
    if scope == "ps" and any(not r["price_sensitive"] for r in rows):
        log.error("%s..%s: asked for price-sensitive only but got unflagged "
                  "items. The feed is ignoring the filter; stopping rather "
                  "than mislabel the corpus.", start, end)
        return None

    # The feed ignores parameters it does not recognise. If the date filter
    # ever stops being honoured it will hand back the live feed instead of
    # history, and that must not be filed under the requested days.
    lo = (start - ONE_DAY).isoformat()
    hi = (end + ONE_DAY).isoformat()
    stray = [r for r in rows if not lo <= r["syd_date"] <= hi]
    if stray:
        log.error("%s..%s: %s of %s items fall outside the window (e.g. %s "
                  "on %s). The feed is ignoring the date filter; stopping "
                  "rather than corrupt the corpus.", start, end, len(stray),
                  len(rows), stray[0]["doc_key"], stray[0]["syd_date"])
        return None

    histstore.upsert_announcements(rows)

    per_day = Counter(r["syd_date"] for r in rows)
    day_counts = {}
    day = start
    while day <= end:
        day_counts[day.isoformat()] = per_day.get(day.isoformat(), 0)
        day += ONE_DAY
    histstore.mark_days(day_counts, scope)
    return len(rows)


def run(start, end, scope="all", progress=None):
    """Harvest [start, end]. Returns a summary dict; never raises on a feed
    failure, it just reports the windows it could not complete."""
    progress = progress or (lambda msg: log.info("%s", msg))
    histstore.init()

    yesterday = _dt.datetime.now(SYDNEY).date() - ONE_DAY
    if end > yesterday:
        progress(f"end date clamped to {yesterday}: today is still being "
                 "published and would be recorded as complete")
        end = yesterday
    if start > end:
        return {"windows": 0, "failed": 0, "stored": 0}

    done = histstore.harvested_days(scope)
    window_days = (config.HARVEST_WINDOW_DAYS_PS if scope == "ps"
                   else config.HARVEST_WINDOW_DAYS)
    windows = plan_windows(start, end, done, window_days)

    total_days = (end - start).days + 1
    todo_days = sum((b - a).days + 1 for a, b in windows)
    progress(f"harvest {start}..{end} scope={scope}: {todo_days} of "
             f"{total_days} days to fetch in {len(windows)} window(s)")

    summary = {"windows": len(windows), "failed": 0, "stored": 0}
    for n, (a, b) in enumerate(windows, 1):
        stored = harvest_window(a, b, scope)
        if stored is None:
            summary["failed"] += 1
            progress(f"  [{n}/{len(windows)}] {a}..{b}  FAILED, will retry "
                     "on the next run")
            continue
        summary["stored"] += stored
        progress(f"  [{n}/{len(windows)}] {a}..{b}  {stored:>6,} items")
    return summary


def status_text():
    histstore.init()
    c = histstore.coverage()
    if not c["days"]:
        return "Corpus is empty. Run: python -m openclaw.harvest"
    return (f"corpus: {c['first_day']} .. {c['last_day']}  "
            f"({c['days']} days: {c['full_days']} full, {c['ps_days']} "
            f"price-sensitive only)\n"
            f"announcements: {c['announcements']:,}  "
            f"price-sensitive: {c['price_sensitive']:,}  "
            f"tickers: {c['tickers']:,}\n"
            f"database: {config.HISTORY_DB_PATH}")


def _parse_date(text):
    return _dt.date.fromisoformat(text)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=_parse_date,
                    help="first Sydney date, YYYY-MM-DD")
    ap.add_argument("--end", type=_parse_date,
                    help="last Sydney date, YYYY-MM-DD (default yesterday)")
    ap.add_argument("--months", type=int, default=12,
                    help="months back from --end when --start is not given")
    ap.add_argument("--price-sensitive-only", action="store_true",
                    help="about a quarter of the volume; enough for the "
                         "event study, not for a complete archive")
    ap.add_argument("--status", action="store_true",
                    help="show corpus coverage and exit")
    args = ap.parse_args(argv)

    if args.status:
        print(status_text())
        return 0

    end = args.end or (_dt.datetime.now(SYDNEY).date() - ONE_DAY)
    start = args.start or (end - _dt.timedelta(days=round(args.months * 30.44)))
    scope = "ps" if args.price_sensitive_only else "all"

    summary = run(start, end, scope)
    print()
    print(status_text())
    if summary["failed"]:
        print(f"\n{summary['failed']} window(s) failed. Run the same command "
              "again to fill them in.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
