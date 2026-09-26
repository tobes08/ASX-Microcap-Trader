"""
Stage 2: rule-based pre-filter.

Cheap, deterministic gating. No LLM, no judgement calls, no hidden state.
Every announcement gets exactly one decision and one reason code, and the
reason is written to filter_log whether it passed or not - a filter you
cannot audit is a filter you cannot trust.

Checks run cheapest-first. The PDF download is the most expensive step, so
it happens last, after the size ceiling has already thrown out anything too
big to be our universe.
"""
import re
from collections import namedtuple

from . import config
from .log import get

log = get("filter")

Result = namedtuple("Result", "passed reason category market_cap "
                              "body_chars body_unavailable")

_TICKER_RE = re.compile(config.TICKER_PATTERN)
_EXCLUSION_RES = [re.compile(p, re.I) for p in config.HEADLINE_EXCLUSIONS]
_CATEGORY_RES = [(name, re.compile(p, re.I))
                 for name, p in config.CATEGORY_PATTERNS]


def categorise(headline):
    """First matching category wins. None means nothing in the whitelist
    matched, which is itself a rejection."""
    for name, rx in _CATEGORY_RES:
        if rx.search(headline or ""):
            return name
    return None


def excluded_headline(headline):
    for rx in _EXCLUSION_RES:
        if rx.search(headline or ""):
            return rx.pattern
    return None


def slippage_haircut(market_cap):
    if market_cap is None:
        return config.SLIPPAGE_DEFAULT
    for ceiling, haircut in config.SLIPPAGE_TIERS:
        if market_cap < ceiling:
            return haircut
    return config.SLIPPAGE_DEFAULT


def evaluate(rec, body_provider, market_cap_provider):
    """Run the pre-filter.

    body_provider and market_cap_provider are zero-argument callables so the
    network work only happens for candidates that survive the cheap checks.
    body_provider returns (text, error).
    """
    ticker = rec.get("ticker") or ""
    headline = rec.get("headline") or ""

    if not ticker or not _TICKER_RE.match(ticker):
        return Result(False, "not_ordinary_shares", None, None, None, 0)

    if ticker in config.TICKER_EXCLUSIONS:
        return Result(False, "ticker_excluded", None, None, None, 0)

    if config.REQUIRE_PRICE_SENSITIVE and not rec.get("price_sensitive"):
        return Result(False, "not_price_sensitive", None, None, None, 0)

    hit = excluded_headline(headline)
    if hit:
        return Result(False, "headline_excluded", None, None, None, 0)

    category = categorise(headline)
    if category is None:
        return Result(False, "no_category_match", None, None, None, 0)

    if category not in config.TRADEABLE_CATEGORIES:
        return Result(False, "category_not_tradeable", category, None, None, 0)

    mcap = market_cap_provider()
    if mcap is None:
        if config.REJECT_ON_UNKNOWN_MARKET_CAP:
            return Result(False, "mcap_unknown", category, None, None, 0)
    elif mcap > config.MAX_MARKET_CAP:
        return Result(False, "mcap_above_ceiling", category, mcap, None, 0)

    text, err = body_provider()
    if text is None:
        # Extraction failed outright, usually a scanned image PDF. That is
        # not evidence of a thin announcement, so it passes with a flag and
        # the report counts how often it happens.
        log.info("%s body unavailable (%s) - passing with flag", ticker, err)
        return Result(True, "ok", category, mcap, 0, 1)

    if len(text) < config.MIN_BODY_CHARS:
        return Result(False, "thin_body", category, mcap, len(text), 0)

    return Result(True, "ok", category, mcap, len(text), 0)
