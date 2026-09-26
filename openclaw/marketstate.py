"""
ASX market-state classification.

Stage 1 of the pipeline attaches a market-state flag at ingestion, not later,
because it determines execution behaviour downstream.

Note on the holiday list: it is best-effort and only affects the state flag
and the poll cadence. Entry and exit resolution never trusts it - those walk
forward to the first bar that actually exists in the price data, so a wrong
or missing holiday self-heals rather than corrupting the ledger.
"""
from datetime import date, datetime, time, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    raise SystemExit("OpenClaw needs Python 3.9+ (zoneinfo). Install a newer Python.")

SYDNEY = ZoneInfo("Australia/Sydney")
UTC = ZoneInfo("UTC")

PRE_OPEN = "PRE_OPEN"
OPENING_AUCTION = "OPENING_AUCTION"
CONTINUOUS = "CONTINUOUS"
CLOSING_AUCTION = "CLOSING_AUCTION"
AFTER_HOURS = "AFTER_HOURS"
NON_TRADING_DAY = "NON_TRADING_DAY"

OPEN_START = time(10, 0)
OPEN_END = time(10, 10)
CONTINUOUS_END = time(16, 0)
CLOSE_AUCTION_END = time(16, 12)

# ASX observed non-trading weekdays. Extend as new calendars are published.
HOLIDAYS = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 26), date(2026, 4, 3), date(2026, 4, 6),
    date(2026, 6, 8), date(2026, 12, 25), date(2026, 12, 28),
    # 2027
    date(2027, 1, 1), date(2027, 1, 26), date(2027, 3, 26), date(2027, 3, 29),
    date(2027, 4, 26), date(2027, 6, 14), date(2027, 12, 27), date(2027, 12, 28),
}


def parse_feed_timestamp(raw):
    """Parse the feed's ISO timestamp into an aware UTC datetime.

    The feed emits e.g. '2026-06-05T07:38:36.000Z'. Returns None on anything
    unparseable rather than raising, so one malformed item cannot stop a poll.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_sydney(dt_utc):
    return dt_utc.astimezone(SYDNEY)


def is_trading_day(d):
    return d.weekday() < 5 and d not in HOLIDAYS


def next_trading_day(d):
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def classify(dt_utc):
    """Return the ASX market state at the moment this announcement published."""
    if dt_utc is None:
        return AFTER_HOURS
    local = to_sydney(dt_utc)
    if not is_trading_day(local.date()):
        return NON_TRADING_DAY
    t = local.time()
    if t < OPEN_START:
        return PRE_OPEN
    if t < OPEN_END:
        return OPENING_AUCTION
    if t < CONTINUOUS_END:
        return CONTINUOUS
    if t < CLOSE_AUCTION_END:
        return CLOSING_AUCTION
    return AFTER_HOURS


def entry_plan(dt_utc):
    """Decide which bar a virtual fill should be assumed against.

    Returns (target_date, price_field) where price_field is 'open' or 'close'.
    Mirrors the live entry-timing logic:

      PRE_OPEN         -> that day's open (we participate in the auction)
      OPENING_AUCTION  -> wait for 10:10, so the same day's close
      CONTINUOUS       -> next available print after the decision, i.e. close
      CLOSING_AUCTION  -> too late to act, next session's open
      AFTER_HOURS      -> next session's open
      NON_TRADING_DAY  -> next session's open

    The date returned is a target, not a promise. Price resolution walks
    forward from it to the first bar that exists.
    """
    state = classify(dt_utc)
    if dt_utc is None:
        return None, None
    local_date = to_sydney(dt_utc).date()

    if state == PRE_OPEN:
        return local_date, "open"
    if state in (OPENING_AUCTION, CONTINUOUS):
        return local_date, "close"
    if state in (CLOSING_AUCTION, AFTER_HOURS):
        return next_trading_day(local_date), "open"
    # NON_TRADING_DAY
    return next_trading_day(local_date), "open"


def poll_interval(now_utc, active, quiet):
    """Slow the poll right down when nothing is likely to be published."""
    local = to_sydney(now_utc)
    if local.weekday() >= 5:
        return quiet
    if time(7, 0) <= local.time() <= time(19, 0):
        return active
    return quiet
