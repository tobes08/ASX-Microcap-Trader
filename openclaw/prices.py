"""
Price and market-cap access via yfinance.

Two jobs:
  1. Market cap, for the Stage 2 size ceiling. Cached for a week, because it
     moves slowly and the lookup is the slowest thing in the filter.
  2. Daily bars, for assumed entries and horizon exits.

Everything here returns None on failure. A yfinance outage must degrade the
system into "no new trades" rather than into a crash or, worse, a ledger
full of fabricated prices.

Prices are split- and dividend-adjusted (auto_adjust=True). Over a 20-day
horizon a consolidation would otherwise show up as a fake 90% loss, which is
exactly the kind of quiet lie the shadow book must not tell.
"""
import datetime as _dt
import time

from . import config, store
from .log import get

log = get("prices")

_yf = None
_bar_cache = {}          # (ticker, start, end) -> {date: bar}
_bar_cache_stamp = 0.0
_BAR_CACHE_TTL = 900     # seconds


def yf():
    global _yf
    if _yf is None:
        import yfinance
        _yf = yfinance
    return _yf


def yahoo_symbol(ticker):
    return f"{ticker.upper()}.AX"


# ------------------------------------------------------------ market cap ---

def market_cap(ticker):
    """Market capitalisation in AUD, or None if it cannot be established."""
    key = f"mcap:{ticker.upper()}"
    cached = store.cache_get(key, max_age_days=config.MARKET_CAP_TTL_DAYS)
    if cached is not None:
        return cached.get("value")

    value = _fetch_market_cap(ticker)
    store.cache_put(key, {"value": value})
    return value


def _fetch_market_cap(ticker):
    sym = yahoo_symbol(ticker)
    try:
        tk = yf().Ticker(sym)
    except Exception as e:
        log.warning("market_cap %s: could not build Ticker: %s", sym, e)
        return None

    # fast_info first: one lightweight request, no full profile scrape.
    try:
        fi = tk.fast_info
        for attr in ("market_cap", "marketCap"):
            try:
                val = fi[attr] if hasattr(fi, "__getitem__") else None
            except Exception:
                val = None
            if val is None:
                val = getattr(fi, attr, None)
            if val:
                return float(val)
    except Exception:
        pass

    try:
        info = tk.get_info()
        val = info.get("marketCap")
        if val:
            return float(val)
        shares = info.get("sharesOutstanding")
        price = info.get("currentPrice") or info.get("previousClose")
        if shares and price:
            return float(shares) * float(price)
    except Exception as e:
        log.info("market_cap %s: info lookup failed (%s)", sym, e)

    return None


# ------------------------------------------------------------- daily bars ---

def _cache_sweep():
    global _bar_cache, _bar_cache_stamp
    now = time.time()
    if now - _bar_cache_stamp > _BAR_CACHE_TTL:
        _bar_cache = {}
        _bar_cache_stamp = now


def daily_bars(ticker, start, end, attempts=3):
    """Adjusted daily bars for [start, end). Returns {date: {...}} or {}."""
    _cache_sweep()
    key = (ticker.upper(), start.isoformat(), end.isoformat())
    if key in _bar_cache:
        return _bar_cache[key]

    sym = yahoo_symbol(ticker)
    frame = None
    for attempt in range(1, attempts + 1):
        try:
            frame = yf().Ticker(sym).history(
                start=start.isoformat(), end=end.isoformat(),
                interval="1d", auto_adjust=True, actions=False,
                raise_errors=False)
            break
        except Exception as e:
            log.warning("bars %s %s..%s failed (%s/%s): %s",
                        sym, start, end, attempt, attempts, e)
            time.sleep(2 * attempt)

    bars = {}
    if frame is not None and len(frame):
        for idx, row in frame.iterrows():
            try:
                day = idx.date()
            except AttributeError:
                continue
            try:
                bars[day] = {
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row.get("Volume", 0) or 0),
                }
            except (KeyError, TypeError, ValueError):
                continue

    _bar_cache[key] = bars
    return bars


def _today_syd():
    from .marketstate import SYDNEY
    return _dt.datetime.now(SYDNEY).date()


def bar_on_or_after(ticker, target_date, max_days):
    """First trading bar on or after target_date. Returns (date, bar) or
    (None, None) if no bar exists yet within the window."""
    end = min(target_date + _dt.timedelta(days=max_days + 5), _today_syd() + _dt.timedelta(days=1))
    if end <= target_date:
        end = target_date + _dt.timedelta(days=1)
    bars = daily_bars(ticker, target_date, end)
    if not bars:
        return None, None
    for day in sorted(bars):
        if day >= target_date:
            return day, bars[day]
    return None, None


def bars_from(ticker, start_date, trading_days_needed):
    """Ordered list of (date, bar) starting at start_date, long enough to
    count `trading_days_needed` sessions forward. Calendar padding is
    generous because weekends, holidays and suspensions all eat days."""
    span = int(trading_days_needed * 1.9) + 12
    end = min(start_date + _dt.timedelta(days=span),
              _today_syd() + _dt.timedelta(days=1))
    bars = daily_bars(ticker, start_date, end)
    return [(d, bars[d]) for d in sorted(bars) if d >= start_date]


# -------------------------------------------------------------- benchmark ---

_benchmark_choice = None


def benchmark_series(start_date, end_date):
    """(symbol, {date: close}) for the first benchmark index that returns
    data. Preference order is Small Ordinaries, then ASX 300, then ASX 200."""
    global _benchmark_choice
    candidates = ([_benchmark_choice] if _benchmark_choice
                  else config.BENCHMARK_TICKERS)
    for sym in candidates:
        try:
            frame = yf().Ticker(sym).history(
                start=start_date.isoformat(),
                end=(end_date + _dt.timedelta(days=1)).isoformat(),
                interval="1d", auto_adjust=True, actions=False,
                raise_errors=False)
        except Exception:
            continue
        if frame is None or not len(frame):
            continue
        series = {}
        for idx, row in frame.iterrows():
            try:
                series[idx.date()] = float(row["Close"])
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
        if series:
            _benchmark_choice = sym
            return sym, series
    return None, {}


def benchmark_close_on_or_after(series, day):
    for d in sorted(series):
        if d >= day:
            return series[d]
    return None
