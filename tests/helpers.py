"""Builders for fabricated price history and corpus rows."""
import datetime as dt
import itertools
import os

from openclaw import config, histstore

_counter = itertools.count()


def fresh_research_db():
    """Point the research store at a new empty database."""
    config.HISTORY_DB_PATH = os.path.join(
        config.RESEARCH_DIR, f"history_{next(_counter)}.sqlite")
    histstore.init()


def weekdays(start, n):
    out, day = [], start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def payload(days, closes, opens=None, volumes=None, adj_factor=1.0,
            splits=(), shares=()):
    """A price_cache payload. Prices are given split-adjusted, the way
    Yahoo returns them with auto_adjust=False; adj_factor scales the
    dividend-adjusted close relative to the close."""
    bars = []
    for i, day in enumerate(days):
        close = closes[i]
        opn = opens[i] if opens else close
        vol = volumes[i] if volumes else 1_000_000.0
        bars.append([day.isoformat(), opn, max(opn, close) * 1.01,
                     min(opn, close) * 0.99, close, close * adj_factor, vol])
    return {"bars": bars,
            "splits": [[d.isoformat(), r] for d, r in splits],
            "shares": [[d.isoformat(), v] for d, v in shares]}


def corpus_row(doc_key, ticker, headline, published_utc, native_types=(),
               price_sensitive=1):
    """A hist_announcements row, built through the harvester's own mapping
    so the tests exercise the same code as a real harvest."""
    from openclaw import harvest
    row = harvest.to_row({
        "documentKey": doc_key, "symbol": ticker, "headline": headline,
        "date": published_utc, "isPriceSensitive": bool(price_sensitive),
        "announcementTypes": list(native_types)})
    assert row is not None, doc_key
    return row
