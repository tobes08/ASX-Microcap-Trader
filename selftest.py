"""
Self-test: prove the plumbing works before leaving it running for weeks.

Touches no live state. It uses a scratch database, hits the feed read-only,
and prices a couple of known ASX tickers. Run it after any config change.

    python selftest.py
"""
import datetime as _dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openclaw import config  # noqa: E402

# Point every write at a throwaway database before anything else imports it.
_SCRATCH = tempfile.mkdtemp(prefix="openclaw_selftest_")
config.DATA_DIR = _SCRATCH
config.RAW_DIR = os.path.join(_SCRATCH, "raw")
config.LOG_DIR = os.path.join(_SCRATCH, "logs")
config.DB_PATH = os.path.join(_SCRATCH, "selftest.sqlite")
config.LEGACY_SEEN_FILE = os.path.join(_SCRATCH, "nonexistent.txt")

from openclaw import feed, filters, prices, shadowbook, store  # noqa: E402
from openclaw.marketstate import (SYDNEY, classify, entry_plan,  # noqa: E402
                                  parse_feed_timestamp)

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
_results = []


def check(name, status, detail=""):
    _results.append((name, status, detail))
    icon = {PASS: "[ ok ]", FAIL: "[FAIL]", WARN: "[warn]"}[status]
    print(f"{icon} {name}" + (f"  -  {detail}" if detail else ""))


# ------------------------------------------------------------ 1. offline ---

def test_market_state():
    # Sydney is UTC+10 in June. 2026-06-05 is a Friday.
    cases = [
        ("2026-06-04T23:30:00.000Z", "PRE_OPEN", "open"),        # Fri 09:30
        ("2026-06-05T00:05:00.000Z", "OPENING_AUCTION", "close"),  # Fri 10:05
        ("2026-06-05T02:00:00.000Z", "CONTINUOUS", "close"),     # Fri 12:00
        ("2026-06-05T06:05:00.000Z", "CLOSING_AUCTION", "open"),  # Fri 16:05
        ("2026-06-05T09:00:00.000Z", "AFTER_HOURS", "open"),     # Fri 19:00
        ("2026-06-06T02:00:00.000Z", "NON_TRADING_DAY", "open"),  # Saturday
    ]
    bad = []
    for raw, want_state, want_field in cases:
        dt = parse_feed_timestamp(raw)
        got_state = classify(dt)
        _, got_field = entry_plan(dt)
        if got_state != want_state or got_field != want_field:
            bad.append(f"{raw} -> {got_state}/{got_field}, "
                       f"expected {want_state}/{want_field}")
    if bad:
        check("market-state classification", FAIL, "; ".join(bad))
    else:
        check("market-state classification", PASS,
              f"{len(cases)} timestamps mapped correctly")


def test_filter_rules():
    def body_ok():
        return "x" * 2000, None

    def body_thin():
        return "x" * 20, None

    def body_scanned():
        return None, "no_extractable_text"

    def cap_small():
        return 30_000_000

    def cap_big():
        return 900_000_000

    def cap_none():
        return None

    def rec(ticker, headline, ps=True):
        return {"ticker": ticker, "headline": headline, "price_sensitive": ps}

    cases = [
        (rec("ABC", "Significant Drilling Results at Foo Prospect"),
         body_ok, cap_small, True, "DRILLING_RESULTS"),
        (rec("ABC", "Change in substantial holding"),
         body_ok, cap_small, False, None),
        (rec("ABC", "Trading Halt"), body_ok, cap_small, False, None),
        (rec("ABC", "Notice of Annual General Meeting"),
         body_ok, cap_small, False, None),
        (rec("ABC", "Contract Award with Major Utility"),
         body_ok, cap_big, False, "CONTRACT_WIN"),
        (rec("ABC", "Significant Drilling Results", ps=False),
         body_ok, cap_small, False, None),
        (rec("ABCD", "Significant Drilling Results"),
         body_ok, cap_small, False, None),
        (rec("ABC", "Some Routine Administrative Update"),
         body_ok, cap_small, False, None),
        (rec("ABC", "Significant Drilling Results"),
         body_thin, cap_small, False, "DRILLING_RESULTS"),
        (rec("ABC", "Significant Drilling Results"),
         body_scanned, cap_small, True, "DRILLING_RESULTS"),
        (rec("ABC", "Significant Drilling Results"),
         body_ok, cap_none, False, "DRILLING_RESULTS"),
        (rec("ABC", "Placement to Sophisticated Investors"),
         body_ok, cap_small, False, "CAPITAL_RAISE"),
    ]
    bad = []
    for r, body, cap, want_pass, want_cat in cases:
        res = filters.evaluate(r, body, cap)
        if res.passed != want_pass:
            bad.append(f"{r['headline'][:38]!r} -> passed={res.passed} "
                       f"({res.reason}), expected {want_pass}")
        elif want_cat and res.category != want_cat:
            bad.append(f"{r['headline'][:38]!r} -> {res.category}, "
                       f"expected {want_cat}")
    if bad:
        check("pre-filter rules", FAIL, " | ".join(bad))
    else:
        check("pre-filter rules", PASS, f"{len(cases)} cases correct")


def test_haircuts():
    pairs = [(10_000_000, 0.030), (49_999_999, 0.030),
             (75_000_000, 0.015), (99_000_000, 0.015),
             (None, 0.030)]
    bad = [f"{c}->{filters.slippage_haircut(c)}"
           for c, want in pairs if filters.slippage_haircut(c) != want]
    check("slippage tiers", FAIL if bad else PASS,
          "; ".join(bad) if bad else "3% sub-$50m, 1.5% $50-100m")


def test_bar_finality():
    today = _dt.datetime.now(SYDNEY).date()
    yesterday = today - _dt.timedelta(days=1)
    tomorrow = today + _dt.timedelta(days=1)
    ok = (shadowbook.bar_is_final(yesterday, "close") is True
          and shadowbook.bar_is_final(tomorrow, "close") is False)
    check("bar finality guard", PASS if ok else FAIL,
          "past bars settled, future bars refused")


def test_database():
    store.init()
    store.record_announcement({
        "doc_key": "SELFTEST-1", "ticker": "ZZZ", "headline": "test",
        "published_utc": None, "published_syd": None, "market_state": "TEST",
        "price_sensitive": 1, "pdf_url": None, "body_chars": 100,
        "body_unavailable": 0, "first_seen_utc": store.utcnow()})
    ok = store.already_seen("SELFTEST-1") and not store.already_seen("NOPE")
    store.cache_put("selftest", {"value": 42})
    ok = ok and store.cache_get("selftest")["value"] == 42
    check("sqlite persistence and dedup", PASS if ok else FAIL,
          f"scratch db at {config.DB_PATH}")


def test_end_to_end_ledger():
    """Drive a whole trade through the ledger with fabricated prices, so the
    arithmetic is verified without needing the market to move."""
    import openclaw.prices as P
    entry_day = _dt.date(2026, 3, 2)
    fake = {entry_day + _dt.timedelta(days=i): {
        "open": 1.00, "high": 1.2, "low": 0.9,
        "close": 1.00 * (1.10 ** min(i, 21)), "volume": 1e6}
        for i in range(0, 40)}

    orig_bars, orig_after, orig_from, orig_bench, orig_final = (
        P.daily_bars, P.bar_on_or_after, P.bars_from,
        P.benchmark_series, shadowbook.bar_is_final)
    try:
        P.daily_bars = lambda t, s, e, attempts=3: fake
        P.bar_on_or_after = lambda t, d, m: (entry_day, fake[entry_day])
        P.bars_from = lambda t, s, n: [(d, fake[d]) for d in sorted(fake)]
        P.benchmark_series = lambda s, e: ("^TEST", {
            d: 100.0 for d in sorted(fake)})
        shadowbook.bar_is_final = lambda day, field: True

        trade_id = store.open_trade({
            "doc_key": "SELFTEST-TRADE", "ticker": "ZZZ",
            "category": "DRILLING_RESULTS", "headline": "test",
            "decision_utc": "2026-03-01T23:00:00+00:00",
            "market_state": "PRE_OPEN", "market_cap": 30_000_000,
            "haircut": 0.03, "entry_target_date": entry_day.isoformat(),
            "entry_field": "open", "notional": 5000.0})
        summary = shadowbook.settle()
        legs = {r["horizon"]: r for r in store.legs_for_trade(trade_id)}

        problems = []
        if summary.get("entered") != 1:
            problems.append(f"entry not resolved ({summary})")
        if set(legs) != set(config.HORIZONS):
            problems.append(f"legs {sorted(legs)} != {config.HORIZONS}")
        else:
            # entry 1.00 * 1.03 = 1.03 ; t+1 close 1.10 * 0.97 = 1.067
            want = (1.10 * 0.97) / 1.03 - 1.0
            got = legs[1]["ret_pct"] / 100.0
            if abs(got - want) > 1e-9:
                problems.append(f"t+1 return {got:.6f} != {want:.6f}")
            if legs[1]["excess_pct"] is None:
                problems.append("benchmark excess not computed")
        row = store.connect().execute(
            "SELECT status FROM trades WHERE id=?", (trade_id,)).fetchone()
        if row["status"] != "CLOSED":
            problems.append(f"status {row['status']} != CLOSED")

        check("end-to-end ledger arithmetic", FAIL if problems else PASS,
              "; ".join(problems) if problems else
              "entry, both haircuts, 3 horizons, benchmark excess")
    finally:
        P.daily_bars, P.bar_on_or_after, P.bars_from, P.benchmark_series = (
            orig_bars, orig_after, orig_from, orig_bench)
        shadowbook.bar_is_final = orig_final


# ------------------------------------------------------------- 2. online ---

def test_feed():
    items = feed.fetch_page(0, max_attempts=2)
    if items is None:
        check("ASX feed reachable", FAIL,
              "no response - check network, or the endpoint/headers have "
              "changed")
        return None
    if not items:
        check("ASX feed reachable", WARN, "connected but returned 0 items")
        return []
    sample = feed.normalise(items[0])
    have = all(sample.get(k) for k in ("doc_key", "ticker", "headline"))
    check("ASX feed reachable", PASS if have else WARN,
          f"{len(items)} items, newest: {sample['ticker']} "
          f"{sample['headline'][:44]}")
    return items


def test_pdf(items):
    if not items:
        check("PDF text extraction", WARN, "skipped, no feed items")
        return
    tried = 0
    for item in items[:6]:
        key = item.get("documentKey")
        if not key:
            continue
        tried += 1
        text, err = feed.extract_pdf_text(key)
        if text:
            check("PDF text extraction", PASS,
                  f"{len(text)} chars from {item.get('symbol')}")
            return
    check("PDF text extraction", WARN,
          f"no extractable text in {tried} attempts - common with scanned "
          "announcements, but check if it persists")


def test_prices(items):
    ticker = None
    for item in (items or []):
        sym = (item.get("symbol") or "").strip().upper()
        if len(sym) == 3:
            ticker = sym
            break
    ticker = ticker or "BHP"

    cap = prices.market_cap(ticker)
    check("market cap lookup", PASS if cap else FAIL,
          f"{ticker} = ${cap / 1e6:.1f}m" if cap else
          f"{ticker} returned nothing - every announcement will be rejected "
          "as mcap_unknown")

    end = _dt.datetime.now(SYDNEY).date()
    start = end - _dt.timedelta(days=20)
    bars = prices.daily_bars(ticker, start, end)
    check("daily bars", PASS if bars else FAIL,
          f"{len(bars)} sessions for {ticker}" if bars else
          f"no price history for {ticker}")

    sym, series = prices.benchmark_series(start, end)
    check("benchmark index", PASS if series else WARN,
          f"{sym}, {len(series)} sessions" if series else
          "no benchmark data - excess return will be blank")


def test_live_filter_sample(items):
    """Run the real filter over the current feed page, but with the network
    steps stubbed out, so we can see the shape of the rejections."""
    if not items:
        check("filter over live feed", WARN, "skipped, no feed items")
        return
    from collections import Counter
    tally = Counter()
    for item in items:
        rec = feed.normalise(item)
        res = filters.evaluate(rec, lambda: ("x" * 5000, None),
                               lambda: 30_000_000)
        tally[res.reason] += 1
    top = ", ".join(f"{k}={v}" for k, v in tally.most_common(5))
    passed = tally.get("ok", 0)
    check("filter over live feed", PASS,
          f"{passed}/{len(items)} would pass on headline+flag alone  ({top})")


def run(online=True):
    print("=" * 70)
    print("OpenClaw self-test")
    print("=" * 70)
    print("\n-- offline logic --")
    test_market_state()
    test_filter_rules()
    test_haircuts()
    test_bar_finality()
    test_database()
    test_end_to_end_ledger()

    if online:
        print("\n-- live services --")
        items = test_feed()
        test_pdf(items)
        test_prices(items)
        test_live_filter_sample(items)
    else:
        print("\n-- live services skipped (--offline) --")

    fails = [n for n, s, _ in _results if s == FAIL]
    warns = [n for n, s, _ in _results if s == WARN]
    print("\n" + "=" * 70)
    if fails:
        print(f"FAILED: {len(fails)} check(s) -> {', '.join(fails)}")
        print("Do not leave it running until these are fixed.")
    else:
        print(f"All {len(_results)} checks passed"
              + (f" ({len(warns)} warning(s): {', '.join(warns)})"
                 if warns else ""))
    print(f"scratch dir: {_SCRATCH}")
    print("=" * 70)
    return not fails


if __name__ == "__main__":
    sys.exit(0 if run(online="--offline" not in sys.argv) else 1)
