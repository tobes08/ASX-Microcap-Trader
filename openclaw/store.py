"""
SQLite persistence.

Design rules:
  * WAL mode, so a reader (the report script) never blocks the writer.
  * Every write commits immediately. A hard power-off loses at most the item
    currently in flight, never the ledger.
  * Schema is created idempotently on every startup, so upgrading is just
    replacing the files.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    doc_key         TEXT PRIMARY KEY,
    ticker          TEXT,
    headline        TEXT,
    published_utc   TEXT,
    published_syd   TEXT,
    market_state    TEXT,
    price_sensitive INTEGER,
    pdf_url         TEXT,
    body_chars      INTEGER,
    body_unavailable INTEGER DEFAULT 0,
    first_seen_utc  TEXT
);
CREATE INDEX IF NOT EXISTS idx_ann_ticker ON announcements(ticker);
CREATE INDEX IF NOT EXISTS idx_ann_pub ON announcements(published_utc);

CREATE TABLE IF NOT EXISTS filter_log (
    doc_key       TEXT PRIMARY KEY,
    ticker        TEXT,
    headline      TEXT,
    decision      TEXT,      -- PASS | REJECT
    reason        TEXT,      -- rejection reason code, or 'ok'
    category      TEXT,
    market_cap    REAL,
    evaluated_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_filter_reason ON filter_log(reason);

CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_key           TEXT UNIQUE,
    ticker            TEXT,
    category          TEXT,
    headline          TEXT,
    decision_utc      TEXT,
    market_state      TEXT,
    market_cap        REAL,
    haircut           REAL,
    entry_target_date TEXT,
    entry_field       TEXT,
    entry_bar_date    TEXT,
    entry_price_raw   REAL,
    entry_price_net   REAL,
    shares            REAL,
    notional          REAL,
    status            TEXT,   -- PENDING_ENTRY | OPEN | CLOSED | VOID
    void_reason       TEXT,
    benchmark_ticker  TEXT,
    benchmark_entry   REAL,
    last_mark_date    TEXT,
    last_mark_price   REAL,
    unrealised_pct    REAL,
    created_utc       TEXT,
    updated_utc       TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);

CREATE TABLE IF NOT EXISTS trade_legs (
    trade_id      INTEGER,
    horizon       INTEGER,
    exit_bar_date TEXT,
    exit_price_raw REAL,
    exit_price_net REAL,
    ret_pct       REAL,
    pnl_aud       REAL,
    bench_ret_pct REAL,
    excess_pct    REAL,
    closed_utc    TEXT,
    PRIMARY KEY (trade_id, horizon)
);

CREATE TABLE IF NOT EXISTS kv_cache (
    key         TEXT PRIMARY KEY,
    value_json  TEXT,
    fetched_utc TEXT
);

CREATE TABLE IF NOT EXISTS runtime (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    """One connection per thread, created on demand."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


def init():
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------- dedup ---

def already_seen(doc_key):
    row = connect().execute(
        "SELECT 1 FROM announcements WHERE doc_key = ?", (doc_key,)
    ).fetchone()
    return row is not None


def load_seen_keys():
    return {
        r[0] for r in connect().execute("SELECT doc_key FROM announcements")
    }


def import_legacy_seen_file(path):
    """One-off migration of the 0.1 append-only seen_keys.txt.

    Marked done in the runtime table so it never runs twice, even if the
    text file is still sitting there.
    """
    conn = connect()
    if get_runtime("legacy_seen_imported"):
        return 0
    if not os.path.exists(path):
        set_runtime("legacy_seen_imported", utcnow())
        return 0
    now = utcnow()
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            key = line.strip()
            if key:
                rows.append((key, None, None, None, None, None, None, None,
                             None, 0, now))
    conn.executemany(
        "INSERT OR IGNORE INTO announcements "
        "(doc_key, ticker, headline, published_utc, published_syd, "
        " market_state, price_sensitive, pdf_url, body_chars, "
        " body_unavailable, first_seen_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    set_runtime("legacy_seen_imported", now)
    return len(rows)


# --------------------------------------------------------- announcements ---

def record_announcement(rec):
    conn = connect()
    conn.execute(
        "INSERT OR REPLACE INTO announcements "
        "(doc_key, ticker, headline, published_utc, published_syd, "
        " market_state, price_sensitive, pdf_url, body_chars, "
        " body_unavailable, first_seen_utc) "
        "VALUES (:doc_key,:ticker,:headline,:published_utc,:published_syd,"
        " :market_state,:price_sensitive,:pdf_url,:body_chars,"
        " :body_unavailable,:first_seen_utc)", rec)
    conn.commit()


def record_filter(doc_key, ticker, headline, decision, reason, category,
                  market_cap):
    conn = connect()
    conn.execute(
        "INSERT OR REPLACE INTO filter_log "
        "(doc_key, ticker, headline, decision, reason, category, market_cap, "
        " evaluated_utc) VALUES (?,?,?,?,?,?,?,?)",
        (doc_key, ticker, headline, decision, reason, category, market_cap,
         utcnow()))
    conn.commit()


# ---------------------------------------------------------------- trades ---

def open_trade(rec):
    conn = connect()
    cur = conn.execute(
        "INSERT OR IGNORE INTO trades "
        "(doc_key, ticker, category, headline, decision_utc, market_state, "
        " market_cap, haircut, entry_target_date, entry_field, notional, "
        " status, created_utc, updated_utc) "
        "VALUES (:doc_key,:ticker,:category,:headline,:decision_utc,"
        " :market_state,:market_cap,:haircut,:entry_target_date,:entry_field,"
        " :notional,'PENDING_ENTRY',:now,:now)",
        dict(rec, now=utcnow()))
    conn.commit()
    return cur.lastrowid


def trades_by_status(*statuses):
    q = ",".join("?" for _ in statuses)
    return connect().execute(
        f"SELECT * FROM trades WHERE status IN ({q}) ORDER BY id", statuses
    ).fetchall()


def update_trade(trade_id, **fields):
    if not fields:
        return
    fields["updated_utc"] = utcnow()
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    conn = connect()
    conn.execute(f"UPDATE trades SET {sets} WHERE id = :id",
                 dict(fields, id=trade_id))
    conn.commit()


def record_leg(trade_id, horizon, **fields):
    conn = connect()
    cols = ["trade_id", "horizon"] + list(fields)
    vals = [trade_id, horizon] + [fields[k] for k in fields]
    placeholders = ",".join("?" for _ in cols)
    conn.execute(
        f"INSERT OR REPLACE INTO trade_legs ({','.join(cols)}) "
        f"VALUES ({placeholders})", vals)
    conn.commit()


def legs_for_trade(trade_id):
    return connect().execute(
        "SELECT * FROM trade_legs WHERE trade_id = ? ORDER BY horizon",
        (trade_id,)).fetchall()


def closed_horizons(trade_id):
    return {r["horizon"] for r in legs_for_trade(trade_id)}


# ----------------------------------------------------------------- cache ---

def cache_get(key, max_age_days=None):
    row = connect().execute(
        "SELECT value_json, fetched_utc FROM kv_cache WHERE key = ?",
        (key,)).fetchone()
    if row is None:
        return None
    if max_age_days is not None:
        try:
            fetched = datetime.fromisoformat(row["fetched_utc"])
        except (TypeError, ValueError):
            return None
        age = (datetime.now(timezone.utc) - fetched).days
        if age > max_age_days:
            return None
    try:
        return json.loads(row["value_json"])
    except (TypeError, ValueError):
        return None


def cache_put(key, value):
    conn = connect()
    conn.execute(
        "INSERT OR REPLACE INTO kv_cache (key, value_json, fetched_utc) "
        "VALUES (?,?,?)", (key, json.dumps(value), utcnow()))
    conn.commit()


# --------------------------------------------------------------- runtime ---

def set_runtime(key, value):
    conn = connect()
    conn.execute("INSERT OR REPLACE INTO runtime (key, value) VALUES (?,?)",
                 (key, str(value)))
    conn.commit()


def get_runtime(key):
    row = connect().execute("SELECT value FROM runtime WHERE key = ?",
                            (key,)).fetchone()
    return row["value"] if row else None


# ----------------------------------------------------------- raw archive ---

def archive_raw(item, published_syd_date):
    """Append the untouched feed JSON to a dated JSONL file.

    Append-only and line-delimited, so a crash mid-write costs one line and
    the rest of the archive stays readable.
    """
    os.makedirs(config.RAW_DIR, exist_ok=True)
    day = published_syd_date or datetime.now(timezone.utc).date().isoformat()
    path = os.path.join(config.RAW_DIR, f"{day}.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, default=str, ensure_ascii=False) + "\n")
