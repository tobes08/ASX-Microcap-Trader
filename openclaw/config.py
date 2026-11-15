"""
OpenClaw configuration.

Every tunable lives here. Nothing else in the codebase should hardcode a
threshold. Edit this file, restart the process, done.
"""
import os

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
LOG_DIR = os.path.join(DATA_DIR, "logs")
DB_PATH = os.path.join(DATA_DIR, "openclaw.sqlite")

# Legacy dedup file from the 0.1 poller. Imported once on first run.
LEGACY_SEEN_FILE = os.path.join(BASE_DIR, "seen_keys.txt")

# --------------------------------------------------------------------------
# Feed
# --------------------------------------------------------------------------
API_URL = "https://asx.api.markitdigital.com/asx-research/1.0/markets/announcements"
PDF_URL_TEMPLATE = "https://asx.api.markitdigital.com/asx-research/1.0/file/{key}"

ITEMS_PER_PAGE = 50
# Pages to sweep each cycle. 1 is plenty at a 60s interval; 2 gives a safety
# margin if a burst of >50 announcements lands between polls.
PAGES_PER_POLL = 2

# Poll cadence. Announcements land outside market hours too, so we never stop
# polling entirely, we just slow down when nothing is likely to arrive.
POLL_INTERVAL_ACTIVE = 60      # seconds, 07:00-19:00 Sydney on weekdays
POLL_INTERVAL_QUIET = 600      # seconds, everything else

HTTP_TIMEOUT = 25
MAX_FETCH_ATTEMPTS = 4
PDF_MAX_BYTES = 15 * 1024 * 1024

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-AU,en;q=0.9",
    "Origin": "https://www2.asx.com.au",
    "Referer": "https://www2.asx.com.au/",
}

# --------------------------------------------------------------------------
# Stage 2 pre-filter
# --------------------------------------------------------------------------
REQUIRE_PRICE_SENSITIVE = True

# Hard ceiling in AUD. Above this the announcement is not our universe.
MAX_MARKET_CAP = 100_000_000

# If the market cap cannot be resolved, reject rather than guess. The report
# surfaces the mcap_unknown count so a systemic yfinance failure is visible
# rather than silently starving the book.
REJECT_ON_UNKNOWN_MARKET_CAP = True

# Minimum extracted body length. Strips cover-page-only admin notices.
# A PDF whose text extraction FAILS outright (scanned image) is handled
# separately: it is allowed through with body_unavailable=1 rather than
# rejected, because failure to extract is not evidence of a thin document.
MIN_BODY_CHARS = 400

# ASX codes are 3 characters for ordinary shares. 4+ characters means options,
# warrants, notes, or an ETF class we do not trade.
#
# Three characters, not three letters: newer listings carry digits (MA1, 4DX,
# A1M, S32) and those skew small, which is exactly this universe. A
# letters-only pattern was discarding them as "not ordinary shares": 1,053 of
# 10,000 feed items in the three weeks to 14 Nov 2026, across 230 companies.
TICKER_PATTERN = r"^[A-Z0-9]{3}$"

# Tickers we never trade regardless of what they announce.
TICKER_EXCLUSIONS = set()

# --------------------------------------------------------------------------
# Announcement categories
# --------------------------------------------------------------------------
# Order matters: the first category whose pattern matches wins, so put the
# more specific categories first.
#
# These category labels are the join key against the Sprint 0.2 event study.
# If you rename one here, rename it there too.
CATEGORY_PATTERNS = [
    ("CORPORATE_ACTION", r"\b(takeover|scheme of arrangement|merger|"
                         r"indicative proposal|binding (heads of agreement|agreement)|"
                         r"acquisition of|proposed acquisition|change of control)\b"),
    ("DRILLING_RESULTS", r"\b(drill(ing)? results?|assay|intercept|intersects?|"
                         r"rc drilling|diamond drill|high[- ]grade|mineralisation|"
                         r"exploration (results|update))\b"),
    ("RESOURCE_UPGRADE", r"\b(mineral resource|jorc|resource estimate|ore reserve|"
                         r"resource upgrade|maiden resource|scoping study|"
                         r"(pre)?[- ]?feasibility study)\b"),
    ("CLINICAL_REGULATORY", r"\b(fda|tga|ema|clinical (trial|data|results)|"
                            r"phase (1|2|3|i|ii|iii)\b|topline|"
                            r"patent grant(ed)?|regulatory approval|ce mark|"
                            r"510\(k\)|marketing authorisation)\b"),
    ("CONTRACT_WIN", r"\b(contract award|awarded (a )?contract|contract win|"
                     r"secures? (a )?contract|new contract|offtake|"
                     r"letter of intent|purchase order|supply agreement|"
                     r"strategic partnership|term sheet)\b"),
    ("PRODUCTION", r"\b(first production|maiden production|commission(ing|ed)|"
                   r"production update|first ore|first gold|nameplate|"
                   r"plant restart)\b"),
    ("EARNINGS", r"\b(appendix 4c|appendix 4d|appendix 4e|half[- ]year(ly)? "
                 r"(results|report)|full[- ]year(ly)? (results|report)|"
                 r"quarterly (activities|report|cashflow|update)|"
                 r"trading update|earnings|guidance|revenue update|"
                 r"record (revenue|sales|quarter))\b"),
    # Capital raises are material but structurally negative for a long-only
    # book. Kept as a category so they can be studied, disabled by default.
    ("CAPITAL_RAISE", r"\b(capital raising|placement|entitlement offer|"
                      r"rights issue|share purchase plan|spp\b)\b"),
]

# Categories the shadow book will actually take a virtual position in.
TRADEABLE_CATEGORIES = {
    "CORPORATE_ACTION",
    "DRILLING_RESULTS",
    "RESOURCE_UPGRADE",
    "CLINICAL_REGULATORY",
    "CONTRACT_WIN",
    "PRODUCTION",
    "EARNINGS",
}

# Always reject, even if a category pattern also matches. Administrative
# noise, index/holding notices, and halt events (halts belong to the Stage 9
# status stream, they are not trade triggers).
HEADLINE_EXCLUSIONS = [
    r"substantial holder",
    r"substantial holding",
    r"change of director'?s? interest",
    r"appendix 3[xyz]\b",
    r"appendix 2a\b",
    r"appendix 4g\b",
    r"notice of (annual )?(general )?meeting",
    r"results of (annual )?(general )?meeting",
    r"\bproxy\b",
    r"cleansing (notice|statement)",
    r"application for quotation",
    r"notification (regarding|of) (unquoted|cessation)",
    r"trading halt",
    r"suspension from (official )?quotation",
    r"reinstatement to (official )?quotation",
    r"pause in trading",
    r"daily fund update",
    r"net tangible asset",
    r"\bnta\b",
    r"investor (presentation|webinar|briefing)",
    r"corporate presentation",
    r"annual report",
    r"corporate governance",
    r"constitution",
    r"dividend",
    r"distribution announcement",
    r"response to asx (price|aware) query",
    r"asx (price|aware) query",
    r"becoming a substantial",
    r"ceasing to be a substantial",
    r"change in substantial",
    r"director appointment|appointment of director|board (change|appointment)",
    r"company secretary",
    r"registered office",
    r"section 708a",
    r"on-?market buy-?back",
    r"appendix 3b\b",
]

# --------------------------------------------------------------------------
# Shadow book
# --------------------------------------------------------------------------
# Tiered slippage haircut, applied on BOTH sides of the trade. This is the
# conservative floor that stops shadow P&L from being fantasy on a thin book.
# (upper_market_cap_exclusive, haircut_fraction)
SLIPPAGE_TIERS = [
    (50_000_000, 0.030),
    (100_000_000, 0.015),
]
SLIPPAGE_DEFAULT = 0.030

# Trading-day horizons measured from the entry bar.
HORIZONS = [1, 5, 20]

# Notional per virtual trade, in AUD. Only affects the dollar columns in the
# report, not the percentage returns.
NOTIONAL_PER_TRADE = 5_000.0

# Benchmark for excess return, in preference order.
BENCHMARK_TICKERS = ["^AXSO", "^AXKO", "^AXJO"]

# How often the settlement pass runs (resolve entries, mark open positions,
# close due horizons). Daily bars only, so there is nothing to gain from
# running this more often than a few times an hour.
SETTLEMENT_INTERVAL = 1800  # seconds

# Price cache TTL. Market caps move slowly; daily bars for a past date never
# change once the session has closed.
MARKET_CAP_TTL_DAYS = 7

# Give up on a trade whose entry bar never materialises (delisting, prolonged
# suspension). Recorded as VOID, not as a loss.
ENTRY_RESOLUTION_MAX_DAYS = 10

# --------------------------------------------------------------------------
# Research (Sprint 0.2): historical harvest and event study
# --------------------------------------------------------------------------
# Everything research-side lives in its own directory and its own database,
# so nothing here can touch the live shadow-book ledger. Both are gitignored:
# the corpus is for private, personal use and must never be committed.
RESEARCH_DIR = os.path.join(DATA_DIR, "research")
HISTORY_DB_PATH = os.path.join(RESEARCH_DIR, "history.sqlite")

# The feed serves at most this many items per query however it is paged
# (probed 15 Nov 2026: page 5 of 2000 comes back empty with count=0). History
# is therefore reached with date windows, each small enough to sit under it.
FEED_MAX_RESULT_WINDOW = 10_000
HARVEST_ITEMS_PER_PAGE = 2000
# Window length in days. A window that hits the cap is split automatically.
HARVEST_WINDOW_DAYS = 7           # every announcement
HARVEST_WINDOW_DAYS_PS = 28       # price-sensitive only
# Minimum gap between harvest requests. The endpoint is unofficial and has no
# contract behind it. A year of history is ~100 requests, so there is nothing
# to gain from going faster and an endpoint to lose.
HARVEST_MIN_INTERVAL = 2.5        # seconds

# Price history for the event study. Extra calendar days fetched before the
# first event (market cap needs prior closes) and after the last (exits).
STUDY_PRICE_PAD_BEFORE_DAYS = 20
STUDY_PRICE_WORKERS = 3           # concurrent Yahoo requests, keep it gentle
# A cached series is reused when it falls this many days short of the end of
# the range asked for. The study asks for prices up to "yesterday", which
# moves every day, so an exact-cover rule would throw the whole cache away
# each morning (found the hard way: 1,900 tickers re-downloaded). Beyond the
# slack the series is fetched again, because Yahoo restates adjusted prices
# whenever a dividend goes ex, so tails cannot be safely appended.
STUDY_PRICE_CACHE_SLACK_DAYS = 7
# Market cap at announcement time is the median of this many prior sessions
# of (shares on issue x close), which shrugs off a one-day data glitch.
STUDY_MCAP_LOOKBACK_SESSIONS = 5

# Liquidity as known at decision time: the median daily turnover (AUD) over
# this many sessions strictly before the entry session. Looking at the entry
# session itself would let the announcement's own volume pick the sample.
LIQUIDITY_LOOKBACK_SESSIONS = 20
LIQUIDITY_MIN_SESSIONS = 5
LIQUIDITY_BANDS = [
    (10_000, "under $10k a day"),
    (50_000, "$10k - $50k"),
    (250_000, "$50k - $250k"),
    (1_000_000, "$250k - $1m"),
    (float("inf"), "over $1m a day"),
]

# Placebo control: for every real trade, this many virtual trades in the same
# stock on pre-open mornings drawn from the same season. Fixed seed, so the
# report is reproducible run to run.
STUDY_PLACEBO_DRAWS = 3
STUDY_PLACEBO_SEED = 7
# A placebo morning must be more than EXCLUDE days from any real announcement
# in that stock: longer than the longest horizon (20 sessions, about 28
# calendar days), so its holding window cannot overlap the real reaction and
# it cannot sit in the post-announcement drift. It is drawn from within
# WINDOW days, so it comes from the same season: micro-cap performance against
# the index swings with reporting seasons, and real announcements cluster in
# them.
STUDY_PLACEBO_EXCLUDE_DAYS = 30
STUDY_PLACEBO_WINDOW_DAYS = 90

# The Sprint 0.2.6 gate, as written in the plan.
GATE_MIN_N = 30
# Cells smaller than this are printed but flagged as anecdote.
STUDY_MIN_N_TO_REPORT = 10

# ASX minimum price steps, as (price_below, tick). Used only by the event
# study's cost stress test: on a 0.7c stock one tick is 14% of the price, so
# a flat percentage haircut understates what crossing the spread costs.
ASX_TICK_SIZES = [
    (0.10, 0.001),
    (2.00, 0.005),
    (float("inf"), 0.01),
]
