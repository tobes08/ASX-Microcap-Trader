"""
Historical prices and point-in-time market cap for the event study.

Two things the live price module does not need and the backtest cannot do
without:

  1. A disk cache. The study prices a couple of thousand tickers; a rerun
     must not ask Yahoo for any of them again.

  2. Market cap as it stood on the day, not as it stands now. Micro caps
     issue shares constantly (one name in the first sample went from 310m to
     2.6bn shares in 17 months), so today's share count times an old price
     can be out by a multiple. Yahoo's share-count history is as reported at
     the time, and its prices are split-adjusted, so the two are reconciled
     through the recorded split ratios before they are multiplied.

Prices come back in two flavours from the same download:

  adjusted  split- and dividend-adjusted, identical to what the live shadow
            book sees through auto_adjust=True. Returns use these.
  quoted    the price that was actually on screen that day. Tick-size and
            market-cap arithmetic use these.

Like the live module, a failure degrades to "no data" for that ticker. It
never invents a price.
"""
import bisect
import datetime as _dt
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config, histstore
from .log import get
from .prices import yahoo_symbol, yf

log = get("histprices")

# bar tuple layout
DATE, OPEN, HIGH, LOW, CLOSE, ADJ_CLOSE, VOLUME = range(7)

# A share-count change that trails a recorded split by a few days is the same
# corporate action seen through two feeds, not a second event.
_SPLIT_SNAP_DAYS = 5
_SPLIT_SNAP_TOLERANCE = 0.20

# Yahoo's recorded split date can trail the day the price level really changed
# by a week or more (JNS: split dated 2 Apr 2025, price already 200x higher by
# 28 Mar). Looking for the price jump this many sessions either side finds it.
_SPLIT_SEARCH_SESSIONS = 10


class PriceSeries:
    """One ticker's daily history with the lookups the event study needs."""

    def __init__(self, ticker, payload):
        self.ticker = ticker
        self.bars = []
        for b in payload.get("bars") or []:
            try:
                day = _dt.date.fromisoformat(b[0])
                row = (day,) + tuple(float(x) for x in b[1:7])
            except (TypeError, ValueError, IndexError):
                continue
            if row[CLOSE] > 0 and row[OPEN] > 0 and row[ADJ_CLOSE] > 0:
                self.bars.append(row)
        self.bars.sort(key=lambda r: r[DATE])
        self.dates = [r[DATE] for r in self.bars]

        self.splits = sorted(
            (_dt.date.fromisoformat(d), float(r))
            for d, r in (payload.get("splits") or []) if r and float(r) > 0)

        shares = sorted(
            (_dt.date.fromisoformat(d), float(v))
            for d, v in (payload.get("shares") or []) if v and float(v) > 0)
        shares = _snap_share_changes_to_splits(shares, self.splits)
        self.share_dates = [d for d, _ in shares]
        self.share_values = [v for _, v in shares]
        self.unapplied_splits = self._find_unapplied_splits()

    def __len__(self):
        return len(self.bars)

    # ------------------------------------------------------------ lookups ---

    def index_on_or_after(self, day):
        i = bisect.bisect_left(self.dates, day)
        return i if i < len(self.dates) else None

    def index_before(self, day):
        """Index of the last bar strictly before day, or None."""
        i = bisect.bisect_left(self.dates, day) - 1
        return i if i >= 0 else None

    def adjusted(self, i, field):
        """Split- and dividend-adjusted price, as auto_adjust=True gives."""
        bar = self.bars[i]
        if field == CLOSE:
            return bar[ADJ_CLOSE]
        return bar[field] * bar[ADJ_CLOSE] / bar[CLOSE]

    def quote_factor(self, day):
        """Multiply a split-adjusted price on `day` by this to recover the
        price that was actually quoted. Product of the ratios of every split
        that took effect after that day."""
        f = 1.0
        for when, ratio in self.splits:
            if when > day:
                f *= ratio
        return f

    def quoted(self, i, field):
        bar = self.bars[i]
        return bar[field] * self.quote_factor(bar[DATE])

    def shares_asof(self, day):
        """Shares on issue as reported on or before `day`.

        Before the first reported figure the earliest one is used, since a
        company's share count rarely shrinks going backwards far enough to
        matter for a size screen. Returns None with no share history at all.
        """
        if not self.share_dates:
            return None
        i = bisect.bisect_right(self.share_dates, day) - 1
        return self.share_values[max(i, 0)]

    def market_cap_before(self, day, sessions=None):
        """Market cap going into `day`: the median, over the last few
        sessions strictly before it, of shares on issue x quoted close.

        Strictly before, so nothing the announcement itself did to the price
        can leak into the size screen. The median is there because the share
        feed and the split feed can disagree by a day around a consolidation,
        which would otherwise show up as a one-day 20x spike.
        """
        sessions = sessions or config.STUDY_MCAP_LOOKBACK_SESSIONS
        end = bisect.bisect_left(self.dates, day)
        values = []
        for i in range(max(0, end - sessions), end):
            bar = self.bars[i]
            shares = self.shares_asof(bar[DATE])
            if shares is None:
                return None
            values.append(shares * bar[CLOSE] * self.quote_factor(bar[DATE]))
        if not values:
            return None
        return statistics.median(values)

    def median_turnover_before(self, day):
        """Median daily turnover in AUD over the sessions strictly before
        `day`, or None when there are too few to say.

        Turnover is split-adjusted close x split-adjusted volume. Yahoo
        restates volume for splits (checked on 104 recorded splits: 98
        behave that way), so multiplying volume by the price that was
        actually quoted would be wrong by the split ratio. Zero-volume
        sessions count as zero, which is the point: that is what an illiquid
        stock looks like.
        """
        end = bisect.bisect_left(self.dates, day)
        window = self.bars[max(0, end - config.LIQUIDITY_LOOKBACK_SESSIONS):end]
        if len(window) < config.LIQUIDITY_MIN_SESSIONS:
            return None
        return statistics.median(b[VOLUME] * b[CLOSE] for b in window)

    def _find_unapplied_splits(self):
        """Dates of price jumps that a recorded split should have removed.

        Yahoo records a split but sometimes never restates the older prices,
        so the series carries the raw jump: a 1-for-200 consolidation shows
        as a 200x one-day move, and a buyer before it "makes" 19,900%. For
        each recorded split, find the largest close-to-close move within a
        few sessions of its date; if it is the size and direction an
        unadjusted split would leave behind, the history is not continuous
        across it. Close, not adjusted close, so a dividend cannot trigger it.
        """
        found = []
        for when, ratio in self.splits:
            expected = math.log(1.0 / ratio)
            if abs(expected) < math.log(1.5):
                continue            # too small to tell from an ordinary move
            i = bisect.bisect_left(self.dates, when)
            lo = max(1, i - _SPLIT_SEARCH_SESSIONS)
            hi = min(len(self.bars) - 1, i + _SPLIT_SEARCH_SESSIONS)
            best_k, best = None, 0.0
            for k in range(lo, hi + 1):
                jump = math.log(self.bars[k][CLOSE] / self.bars[k - 1][CLOSE])
                if abs(jump) > abs(best):
                    best_k, best = k, jump
            if (best_k is not None and best * expected > 0
                    and 0.6 <= best / expected <= 1.5):
                found.append(self.dates[best_k])
        return found

    def discontinuity_between(self, day_a, day_b):
        """True when a return from day_a to day_b crosses a corporate action
        the price data does not handle, so the return would be fiction.

        Two signatures:
          * a recorded split whose price adjustment never reached the
            history (see _find_unapplied_splits);
          * the share count collapsing with no split on record to explain
            it, which is a consolidation Yahoo has missed entirely.
        """
        if any(day_a <= d <= day_b for d in self.unapplied_splits):
            return True
        a = self.shares_asof(day_a)
        b = self.shares_asof(day_b)
        if not a or not b or b / a > 0.5:
            return False
        lo = day_a - _dt.timedelta(days=_SPLIT_SNAP_DAYS)
        hi = day_b + _dt.timedelta(days=_SPLIT_SNAP_DAYS)
        return not any(lo <= when <= hi for when, _ in self.splits)


def _snap_share_changes_to_splits(shares, splits):
    """Move a share-count step that trails a split by a few days back onto
    the split's effective date, so the two feeds agree on when it happened."""
    if not shares or not splits:
        return shares
    out = list(shares)
    for when, ratio in splits:
        for i in range(1, len(out)):
            day, value = out[i]
            if not when < day <= when + _dt.timedelta(days=_SPLIT_SNAP_DAYS):
                continue
            prev = out[i - 1][1]
            if prev and abs(value / prev / ratio - 1.0) <= _SPLIT_SNAP_TOLERANCE:
                out[i] = (when, value)
                break
    # A snapped step can now share a date with an earlier point; keep the
    # later (post-split) figure for that date.
    merged = {}
    for day, value in out:
        merged[day] = value
    return sorted(merged.items())


def tick_size(price):
    """ASX minimum price step for a quoted price."""
    for ceiling, tick in config.ASX_TICK_SIZES:
        if price < ceiling:
            return tick
    return config.ASX_TICK_SIZES[-1][1]


# ------------------------------------------------------------------ fetch ---

def _symbol(ticker):
    return ticker if ticker.startswith("^") else yahoo_symbol(ticker)


_raise_kwargs = None


def _make_yfinance_raise():
    """yfinance swallows errors by default and hands back an empty frame,
    which makes "Yahoo is rate limiting us" indistinguishable from "this
    ticker does not exist". How to switch that off depends on the version."""
    global _raise_kwargs
    if _raise_kwargs is None:
        try:
            yf().config.debug.hide_exceptions = False
            _raise_kwargs = {}
        except AttributeError:
            _raise_kwargs = {"raise_errors": True}
    return _raise_kwargs


def _is_missing_ticker(exc):
    if type(exc).__name__ in ("YFTickerMissingError", "YFTzMissingError",
                              "YFPricesMissingError"):
        return True
    text = str(exc)
    return "404" in text and "Not Found" in text


def _fetch_one(ticker, start, end, attempts=4):
    """Returns (status, payload). status is ok | no_data | error. An error
    is transient and is not cached."""
    kwargs = _make_yfinance_raise()
    sym = _symbol(ticker)
    frame = None
    for attempt in range(1, attempts + 1):
        try:
            tk = yf().Ticker(sym)
            frame = tk.history(
                start=start.isoformat(),
                end=(end + _dt.timedelta(days=1)).isoformat(),
                interval="1d", auto_adjust=False, actions=True, **kwargs)
            break
        except Exception as e:  # noqa: BLE001 - classified below
            if _is_missing_ticker(e):
                return "no_data", None
            rate_limited = type(e).__name__ == "YFRateLimitError"
            if attempt == attempts:
                log.warning("%s: giving up after %s attempts (%s: %s)",
                            sym, attempts, type(e).__name__, str(e)[:120])
                return "error", None
            time.sleep((30 if rate_limited else 3) * attempt)

    if frame is None or not len(frame):
        return "no_data", None

    bars, splits = [], []
    for idx, row in frame.iterrows():
        try:
            day = idx.date().isoformat()
            bars.append([day, float(row["Open"]), float(row["High"]),
                         float(row["Low"]), float(row["Close"]),
                         float(row["Adj Close"]),
                         float(row.get("Volume", 0) or 0)])
            ratio = float(row.get("Stock Splits", 0) or 0)
            if ratio > 0:
                splits.append([day, ratio])
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    bars = [b for b in bars if all(x == x for x in b[1:6])]   # drop NaN rows
    if not bars:
        return "no_data", None

    shares = []
    if not ticker.startswith("^"):
        try:
            series = tk.get_shares_full(
                start=(start - _dt.timedelta(days=45)).isoformat(),
                end=(end + _dt.timedelta(days=1)).isoformat())
        except Exception as e:  # noqa: BLE001 - share history is optional
            if type(e).__name__ == "YFRateLimitError":
                return "error", None
            series = None
        if series is not None and len(series):
            last = None
            for idx, value in series.items():
                try:
                    day = idx.date().isoformat()
                    value = float(value)
                except (AttributeError, TypeError, ValueError):
                    continue
                if value != value or value <= 0:
                    continue
                if shares and shares[-1][0] == day:
                    shares[-1][1] = value        # duplicate stamp, keep last
                elif value != last:
                    shares.append([day, value])  # store change points only
                last = value

    return "ok", {"bars": bars, "splits": splits, "shares": shares}


def _cached(ticker, start, end):
    hit = histstore.price_cache_get(ticker)
    if hit is None:
        return None
    slack = _dt.timedelta(days=config.STUDY_PRICE_CACHE_SLACK_DAYS)
    try:
        covers = (_dt.date.fromisoformat(hit["start_date"]) <= start
                  and _dt.date.fromisoformat(hit["end_date"]) >= end - slack)
    except (TypeError, ValueError):
        return None
    return hit if covers else None


def load_many(tickers, start, end, refresh=False, progress=None):
    """Price history for many tickers over [start, end].

    Returns {ticker: PriceSeries or None}. None means Yahoo has nothing for
    the ticker (delisted, renamed, never covered) or the fetch failed; the
    second dict returned says which, so the caller can report survivorship
    separately from a flaky network.
    """
    progress = progress or (lambda msg: log.info("%s", msg))
    out, status = {}, {}
    todo = []
    for t in sorted(set(tickers)):
        hit = None if refresh else _cached(t, start, end)
        if hit is None:
            todo.append(t)
        else:
            status[t] = hit["status"]
            out[t] = (PriceSeries(t, hit["payload"])
                      if hit["status"] == "ok" else None)

    if todo:
        progress(f"prices: {len(out)} cached, fetching {len(todo)} from "
                 f"Yahoo with {config.STUDY_PRICE_WORKERS} workers")
        done = 0
        with ThreadPoolExecutor(config.STUDY_PRICE_WORKERS) as pool:
            futures = {pool.submit(_fetch_one, t, start, end): t
                       for t in todo}
            for fut in as_completed(futures):
                t = futures[fut]
                try:
                    st, payload = fut.result()
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: fetch crashed (%s)", t, e)
                    st, payload = "error", None
                status[t] = st
                if st != "error":
                    histstore.price_cache_put(t, st, start, end, payload)
                out[t] = PriceSeries(t, payload) if st == "ok" else None
                done += 1
                if done % 100 == 0 or done == len(todo):
                    progress(f"prices: {done}/{len(todo)} fetched")

    for t, series in list(out.items()):
        if series is not None and not len(series):
            out[t], status[t] = None, "no_data"
    return out, status
