"""Developer integration test: drives a full poll cycle and report against a
fabricated feed and fabricated prices. Not part of the shipped self-test.

    python _dev_integration_test.py
"""
import datetime as _dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from openclaw import config

S = tempfile.mkdtemp(prefix="openclaw_integ_")
config.DATA_DIR = S
config.RAW_DIR = os.path.join(S, "raw")
config.LOG_DIR = os.path.join(S, "logs")
config.DB_PATH = os.path.join(S, "t.sqlite")
config.LEGACY_SEEN_FILE = os.path.join(S, "seen.txt")

from openclaw import feed, pipeline, prices, report, shadowbook, store

with open(config.LEGACY_SEEN_FILE, "w") as fh:
    fh.write("LEGACY-KEY-1\nLEGACY-KEY-2\n\n")

TODAY = _dt.date(2026, 8, 27)   # a Thursday

FEED = [
    {"documentKey": "K1", "symbol": "AAA", "headline": "Outstanding Drilling Results at Bar Prospect",
     "date": "2026-08-26T23:30:00.000Z", "isPriceSensitive": True},
    {"documentKey": "K2", "symbol": "BBB", "headline": "Change in substantial holding",
     "date": "2026-08-26T23:31:00.000Z", "isPriceSensitive": True},
    {"documentKey": "K3", "symbol": "CCC", "headline": "Contract Award with State Government",
     "date": "2026-08-27T02:15:00.000Z", "isPriceSensitive": True},
    {"documentKey": "K4", "symbol": "DDD", "headline": "Trading Halt",
     "date": "2026-08-27T02:16:00.000Z", "isPriceSensitive": True},
    {"documentKey": "K5", "symbol": "EEE", "headline": "Quarterly Activities Report",
     "date": "2026-08-27T07:00:00.000Z", "isPriceSensitive": True},
    {"documentKey": "LEGACY-KEY-1", "symbol": "FFF", "headline": "Maiden Resource Estimate",
     "date": "2026-08-27T07:01:00.000Z", "isPriceSensitive": True},
    {"documentKey": "K6", "symbol": "GGG", "headline": "Investor Presentation",
     "date": "2026-08-27T07:02:00.000Z", "isPriceSensitive": True},
    {"documentKey": "K7", "symbol": "HHH", "headline": "High-Grade Assay Results",
     "date": "2026-08-27T07:03:00.000Z", "isPriceSensitive": False},
    {"documentKey": "K8", "symbol": "III", "headline": "Binding Agreement to Acquire Foo Pty Ltd",
     "date": "2026-07-01T07:03:00.000Z", "isPriceSensitive": True},
]

CAPS = {"AAA": 28_000_000, "CCC": 72_000_000, "EEE": 450_000_000,
        "III": 15_000_000}


def fake_bars(ticker, start, end, attempts=3):
    if ticker == "III":
        return {}          # delisted / suspended: exercises the VOID path
    base = {"AAA": 0.50, "CCC": 1.20}.get(ticker, 1.00)
    out = {}
    d = _dt.date(2026, 8, 20)
    step = 0
    while d <= _dt.date(2026, 10, 30):
        if d.weekday() < 5:
            px = base * (1.02 ** step)
            out[d] = {"open": px * 0.99, "high": px * 1.05,
                      "low": px * 0.95, "close": px, "volume": 1e6}
            step += 1
        d += _dt.timedelta(days=1)
    return {k: v for k, v in out.items() if start <= k < end}


feed.fetch_page = lambda page, max_attempts=None: (FEED if page == 0 else [])
feed.extract_pdf_text = lambda key: ("body " * 400, None)
prices.market_cap = lambda t: CAPS.get(t)
prices.daily_bars = fake_bars
_BENCH_WINDOW = sorted(fake_bars("X", _dt.date(2026, 8, 20),
                                 _dt.date(2026, 10, 30)))
prices.benchmark_series = lambda s, e: (
    "^AXSO", {d: 3000.0 * (1.001 ** i) for i, d in enumerate(_BENCH_WINDOW)})
shadowbook.bar_is_final = lambda day, field: day <= _dt.date(2026, 10, 20)
prices._today_syd = lambda: _dt.date(2026, 10, 20)
shadowbook._today_syd = lambda: _dt.datetime(2026, 10, 20, 17, 0)

store.init()
n = store.import_legacy_seen_file(config.LEGACY_SEEN_FILE)
print(f"legacy keys imported: {n}")

print("\n--- cycle 1 ---")
print(pipeline.poll_once())
print("--- cycle 2 (dedup) ---")
print(pipeline.poll_once())

print("\n--- settle ---")
print(shadowbook.settle())

print("\n" + report.build())
print(f"\nraw archive: {sorted(os.listdir(config.RAW_DIR))}")
