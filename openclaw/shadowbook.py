"""
Forward paper simulation - the shadow book.

Forward-only. It starts from now, takes decisions from the live filter, and
marks them to market. No historical replay, no order book, no broker.

Two honesty rules are load-bearing here:

  1. The tiered slippage haircut is applied on both sides of every trade.
     Without an order book the assumed entry price is the only number that
     can quietly lie to you, so it leans pessimistic by design.

  2. A bar is only used once it is final. A daily bar for the current session
     is partial while the market is open - its "close" is just the last
     print. Marking against it would silently invent P&L, so entries and
     exits wait for the session to finish.

The three horizons are parallel hypotheticals on the same event, not a
sequence of positions. t+1, t+5 and t+20 each answer "what if you had exited
there". Aggregate P&L is therefore reported per horizon and never summed
across them.
"""
import datetime as _dt

from . import config, prices, store
from .filters import slippage_haircut
from .log import get
from .marketstate import SYDNEY, entry_plan, parse_feed_timestamp

log = get("shadow")

_OPEN_FINAL_AFTER = _dt.time(10, 20)
_CLOSE_FINAL_AFTER = _dt.time(16, 20)


def _today_syd():
    return _dt.datetime.now(SYDNEY)


def bar_is_final(day, field):
    """Has this bar stopped moving?

    Anything before today is settled. Today's open is fixed shortly after the
    staggered auction finishes; today's close is not fixed until the closing
    auction has run.
    """
    now = _today_syd()
    if day < now.date():
        return True
    if day > now.date():
        return False
    if field == "open":
        return now.time() >= _OPEN_FINAL_AFTER
    return now.time() >= _CLOSE_FINAL_AFTER


# ------------------------------------------------------------- decisions ---

def record_decision(rec, result):
    """Turn a passing announcement into a PENDING_ENTRY virtual trade."""
    published = parse_feed_timestamp(rec.get("published_raw"))
    target_date, field = entry_plan(published)
    if target_date is None:
        log.warning("%s %s: unparseable timestamp, no entry plan",
                    rec.get("ticker"), rec.get("doc_key"))
        return None

    haircut = slippage_haircut(result.market_cap)
    trade_id = store.open_trade({
        "doc_key": rec["doc_key"],
        "ticker": rec["ticker"],
        "category": result.category,
        "headline": rec["headline"],
        "decision_utc": published.isoformat() if published else None,
        "market_state": rec.get("market_state"),
        "market_cap": result.market_cap,
        "haircut": haircut,
        "entry_target_date": target_date.isoformat(),
        "entry_field": field,
        "notional": config.NOTIONAL_PER_TRADE,
    })
    log.info("SHADOW TRADE queued  %s  %s  cap=%s  entry=%s %s  haircut=%.1f%%",
             rec["ticker"], result.category,
             _fmt_cap(result.market_cap), target_date, field, haircut * 100)
    return trade_id


def _fmt_cap(cap):
    if cap is None:
        return "unknown"
    return f"${cap / 1e6:.1f}m"


# ------------------------------------------------------------ settlement ---

def settle():
    """Resolve pending entries, mark open positions, close due horizons.

    Idempotent and safe to run as often as you like. Returns a small summary
    dict for logging.
    """
    summary = {"entered": 0, "voided": 0, "legs_closed": 0, "marked": 0,
               "still_pending": 0}

    bench_symbol, bench_series = _benchmark_window()

    for trade in store.trades_by_status("PENDING_ENTRY"):
        outcome = _resolve_entry(trade, bench_symbol, bench_series)
        summary[outcome] = summary.get(outcome, 0) + 1

    for trade in store.trades_by_status("OPEN"):
        closed, marked = _advance_trade(trade, bench_series)
        summary["legs_closed"] += closed
        summary["marked"] += marked

    store.set_runtime("last_settle_utc", store.utcnow())
    return summary


def _benchmark_window():
    """One benchmark fetch per settlement pass covers every open trade."""
    rows = store.trades_by_status("PENDING_ENTRY", "OPEN")
    if not rows:
        return None, {}
    earliest = None
    for r in rows:
        for value in (r["entry_bar_date"], r["entry_target_date"]):
            if not value:
                continue
            try:
                d = _dt.date.fromisoformat(value)
            except (TypeError, ValueError):
                continue
            if earliest is None or d < earliest:
                earliest = d
    if earliest is None:
        return None, {}
    start = earliest - _dt.timedelta(days=5)
    end = _today_syd().date()
    return prices.benchmark_series(start, end)


def _resolve_entry(trade, bench_symbol, bench_series):
    ticker = trade["ticker"]
    try:
        target = _dt.date.fromisoformat(trade["entry_target_date"])
    except (TypeError, ValueError):
        store.update_trade(trade["id"], status="VOID",
                           void_reason="bad_entry_target")
        return "voided"

    field = trade["entry_field"] or "open"
    day, bar = prices.bar_on_or_after(ticker, target,
                                      config.ENTRY_RESOLUTION_MAX_DAYS)

    if bar is None or not bar_is_final(day, field):
        age = (_today_syd().date() - target).days
        if age > config.ENTRY_RESOLUTION_MAX_DAYS:
            store.update_trade(trade["id"], status="VOID",
                               void_reason="no_entry_bar")
            log.warning("VOID %s: no usable entry bar within %s days of %s",
                        ticker, config.ENTRY_RESOLUTION_MAX_DAYS, target)
            return "voided"
        return "still_pending"

    raw = bar.get(field)
    if not raw or raw <= 0:
        return "still_pending"

    haircut = trade["haircut"] or config.SLIPPAGE_DEFAULT
    net = raw * (1.0 + haircut)          # we pay up to get filled
    notional = trade["notional"] or config.NOTIONAL_PER_TRADE
    shares = notional / net
    bench_entry = prices.benchmark_close_on_or_after(bench_series, day)

    store.update_trade(
        trade["id"], status="OPEN", entry_bar_date=day.isoformat(),
        entry_price_raw=raw, entry_price_net=net, shares=shares,
        benchmark_ticker=bench_symbol, benchmark_entry=bench_entry)

    log.info("ENTERED %s  %s %s @ %.4f (net %.4f after %.1f%% haircut)",
             ticker, day, field, raw, net, haircut * 100)
    return "entered"


def _advance_trade(trade, bench_series):
    ticker = trade["ticker"]
    haircut = trade["haircut"] or config.SLIPPAGE_DEFAULT
    entry_net = trade["entry_price_net"]
    shares = trade["shares"] or 0.0
    bench_entry = trade["benchmark_entry"]

    try:
        entry_day = _dt.date.fromisoformat(trade["entry_bar_date"])
    except (TypeError, ValueError):
        return 0, 0

    max_h = max(config.HORIZONS)
    series = prices.bars_from(ticker, entry_day, max_h)
    if not series:
        log.warning("%s: no bars from %s, cannot advance (suspended, "
                    "delisted, or a price-source outage)", ticker, entry_day)
        return 0, 0
    if series[0][0] != entry_day:
        log.warning("%s: entry bar %s has vanished from the price history "
                    "(first bar now %s), leaving trade untouched",
                    ticker, entry_day, series[0][0])
        return 0, 0

    done = store.closed_horizons(trade["id"])
    closed_now = 0

    for horizon in config.HORIZONS:
        if horizon in done:
            continue
        if horizon >= len(series):
            continue
        exit_day, exit_bar = series[horizon]
        if not bar_is_final(exit_day, "close"):
            continue
        raw = exit_bar.get("close")
        if not raw or raw <= 0:
            continue

        net = raw * (1.0 - haircut)      # we give up the spread on the way out
        ret_pct = (net / entry_net - 1.0) * 100.0
        pnl = shares * (net - entry_net)

        bench_ret = None
        excess = None
        if bench_entry:
            bench_exit = prices.benchmark_close_on_or_after(bench_series,
                                                            exit_day)
            if bench_exit:
                bench_ret = (bench_exit / bench_entry - 1.0) * 100.0
                excess = ret_pct - bench_ret

        store.record_leg(
            trade["id"], horizon,
            exit_bar_date=exit_day.isoformat(), exit_price_raw=raw,
            exit_price_net=net, ret_pct=ret_pct, pnl_aud=pnl,
            bench_ret_pct=bench_ret, excess_pct=excess,
            closed_utc=store.utcnow())
        closed_now += 1
        log.info("CLOSED  %s t+%-2d  %+.2f%%  ($%+.2f)  excess %s",
                 ticker, horizon, ret_pct, pnl,
                 f"{excess:+.2f}%" if excess is not None else "n/a")

    # Mark to the most recent final bar.
    marked = 0
    last_day, last_bar = None, None
    for day, bar in series:
        if bar_is_final(day, "close"):
            last_day, last_bar = day, bar
    if last_bar and entry_net:
        mark_net = last_bar["close"] * (1.0 - haircut)
        store.update_trade(
            trade["id"], last_mark_date=last_day.isoformat(),
            last_mark_price=last_bar["close"],
            unrealised_pct=(mark_net / entry_net - 1.0) * 100.0)
        marked = 1

    if len(store.closed_horizons(trade["id"])) >= len(config.HORIZONS):
        store.update_trade(trade["id"], status="CLOSED")

    return closed_now, marked
