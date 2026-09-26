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

# ASX codes are 3 letters for ordinary shares. 4+ characters means options,
# warrants, notes, or an ETF class we do not trade.
TICKER_PATTERN = r"^[A-Z]{3}$"

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
