"""
Research database (Sprint 0.2).

A separate SQLite file from the live shadow-book ledger, on purpose. The
historical corpus, the price cache and the event-study output can all be
deleted and rebuilt without the forward book noticing, and nothing in the
research tools ever opens data/openclaw.sqlite.

The corpus is for private, personal research. The whole research directory
is gitignored and must stay that way.
"""
import json
import os
import re
import sqlite3
import threading
import zlib
from datetime import datetime, timezone

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS hist_announcements (
    doc_key         TEXT PRIMARY KEY,
    ann_id          TEXT,      -- ASX announcement id, the middle of the key
    pair_suffix     TEXT,      -- PS | NPS on cross-lodged pairs, else NULL
    ticker          TEXT,
    headline        TEXT,
    published_utc   TEXT,
    syd_date        TEXT,      -- Sydney calendar date of publication
    market_state    TEXT,
    price_sensitive INTEGER,
    native_types    TEXT,      -- JSON list: the ASX's own announcement types
    raw_json        TEXT,      -- the feed item, untouched
    harvested_utc   TEXT
);
CREATE INDEX IF NOT EXISTS idx_hist_date ON hist_announcements(syd_date);
CREATE INDEX IF NOT EXISTS idx_hist_ticker ON hist_announcements(ticker);
CREATE INDEX IF NOT EXISTS idx_hist_ann ON hist_announcements(ann_id);

CREATE TABLE IF NOT EXISTS harvest_days (
    syd_date      TEXT PRIMARY KEY,
    scope         TEXT,        -- all | ps (price-sensitive only)
    items         INTEGER,
    harvested_utc TEXT
);

CREATE TABLE IF NOT EXISTS price_cache (
    ticker      TEXT PRIMARY KEY,
    status      TEXT,          -- ok | no_data
    start_date  TEXT,
    end_date    TEXT,
    payload     BLOB,          -- zlib-compressed JSON: bars, splits, shares
    fetched_utc TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# 2924-03100606-6A1329605, or 2924-03100606_PS-6A1329605 when one release is
# cross-lodged under two issuers.
_KEY_RE = re.compile(r"^[^-]+-(\d+)(?:_(PS|NPS))?-")


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def split_doc_key(doc_key):
    """(announcement_id, pair_suffix) from a documentKey.

    A release lodged under two issuers arrives as two documents sharing one
    announcement id: the _PS half is flagged price sensitive for its issuer,
    the _NPS half is not. They are one event, not two. Checked against 10,000
    live items on 15 Nov 2026: 16 pairs, the suffix agreed with the
    isPriceSensitive flag every time.
    """
    m = _KEY_RE.match(doc_key or "")
    if not m:
        return None, None
    return m.group(1), m.group(2)


def connect():
    conn = getattr(_local, "conn", None)
    path = config.HISTORY_DB_PATH
    if conn is None or getattr(_local, "path", None) != path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
        _local.path = path
    return conn


def init():
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ----------------------------------------------------------------- corpus ---

def upsert_announcements(rows):
    """Insert harvested announcements. Re-harvesting a day replaces its rows
    with identical ones, so the operation is idempotent."""
    if not rows:
        return 0
    conn = connect()
    conn.executemany(
        "INSERT OR REPLACE INTO hist_announcements "
        "(doc_key, ann_id, pair_suffix, ticker, headline, published_utc, "
        " syd_date, market_state, price_sensitive, native_types, raw_json, "
        " harvested_utc) "
        "VALUES (:doc_key,:ann_id,:pair_suffix,:ticker,:headline,"
        " :published_utc,:syd_date,:market_state,:price_sensitive,"
        " :native_types,:raw_json,:harvested_utc)", rows)
    conn.commit()
    return len(rows)


def mark_days(day_counts, scope):
    """Record harvested days. A full harvest supersedes a price-sensitive
    one; a price-sensitive harvest never downgrades a full one."""
    conn = connect()
    now = utcnow()
    for day, n in day_counts.items():
        existing = conn.execute(
            "SELECT scope FROM harvest_days WHERE syd_date = ?",
            (day,)).fetchone()
        if existing and existing["scope"] == "all" and scope == "ps":
            continue
        conn.execute(
            "INSERT OR REPLACE INTO harvest_days "
            "(syd_date, scope, items, harvested_utc) VALUES (?,?,?,?)",
            (day, scope, n, now))
    conn.commit()


def harvested_days(scope):
    """Days that already satisfy a harvest of this scope."""
    if scope == "ps":
        q = "SELECT syd_date FROM harvest_days"
    else:
        q = "SELECT syd_date FROM harvest_days WHERE scope = 'all'"
    return {r[0] for r in connect().execute(q)}


def coverage():
    conn = connect()
    row = conn.execute(
        "SELECT MIN(syd_date) lo, MAX(syd_date) hi, COUNT(*) days, "
        "       SUM(scope = 'all') full_days, SUM(scope = 'ps') ps_days "
        "FROM harvest_days").fetchone()
    ann = conn.execute(
        "SELECT COUNT(*) n, SUM(price_sensitive) ps, "
        "       COUNT(DISTINCT ticker) tickers FROM hist_announcements"
    ).fetchone()
    return {"first_day": row["lo"], "last_day": row["hi"],
            "days": row["days"] or 0, "full_days": row["full_days"] or 0,
            "ps_days": row["ps_days"] or 0, "announcements": ann["n"] or 0,
            "price_sensitive": ann["ps"] or 0, "tickers": ann["tickers"] or 0}


def announcements(start, end, price_sensitive_only=True):
    q = ("SELECT * FROM hist_announcements WHERE syd_date >= ? AND "
         "syd_date <= ?")
    if price_sensitive_only:
        q += " AND price_sensitive = 1"
    q += " ORDER BY published_utc, doc_key"
    return connect().execute(q, (start.isoformat(), end.isoformat())).fetchall()


# ------------------------------------------------------------ price cache ---

def price_cache_get(ticker):
    row = connect().execute(
        "SELECT * FROM price_cache WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        return None
    payload = None
    if row["payload"] is not None:
        try:
            payload = json.loads(zlib.decompress(row["payload"]).decode())
        except (zlib.error, ValueError):
            return None
    return {"ticker": row["ticker"], "status": row["status"],
            "start_date": row["start_date"], "end_date": row["end_date"],
            "payload": payload, "fetched_utc": row["fetched_utc"]}


def price_cache_put(ticker, status, start, end, payload):
    blob = None
    if payload is not None:
        blob = zlib.compress(
            json.dumps(payload, separators=(",", ":")).encode(), 6)
    conn = connect()
    conn.execute(
        "INSERT OR REPLACE INTO price_cache "
        "(ticker, status, start_date, end_date, payload, fetched_utc) "
        "VALUES (?,?,?,?,?,?)",
        (ticker, status, start.isoformat(), end.isoformat(), blob, utcnow()))
    conn.commit()


# ------------------------------------------------------------------- meta ---

def set_meta(key, value):
    conn = connect()
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                 (key, json.dumps(value)))
    conn.commit()


def get_meta(key, default=None):
    row = connect().execute("SELECT value FROM meta WHERE key = ?",
                            (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return default
