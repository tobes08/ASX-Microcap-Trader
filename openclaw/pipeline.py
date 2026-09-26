"""
Stage 1 -> Stage 2 -> shadow book, wired together.

One poll cycle: fetch the newest pages, and for every announcement we have
not seen before, archive it, flag its market state, run the pre-filter, and
queue a virtual trade if it survives.

Every announcement is persisted whether it passes or not. The rejects are
the corpus Sprint 0.2's event study will need, and the reason codes are how
you tell a filter that is working from one that is quietly rejecting
everything.
"""
from . import feed, filters, prices, shadowbook, store
from .log import get
from .marketstate import classify, parse_feed_timestamp, to_sydney

log = get("pipeline")


def process_item(item):
    """Returns one of: 'seen', 'skipped', 'rejected', 'passed'."""
    rec = feed.normalise(item)
    doc_key = rec.get("doc_key")
    if not doc_key:
        return "skipped"
    if store.already_seen(doc_key):
        return "seen"

    published = parse_feed_timestamp(rec.get("published_raw"))
    state = classify(published)
    rec["market_state"] = state
    syd_date = to_sydney(published).date().isoformat() if published else None

    store.archive_raw(item, syd_date)

    # Lazy providers: neither runs unless the cheap checks have already passed.
    def body_provider():
        return feed.extract_pdf_text(doc_key)

    def market_cap_provider():
        return prices.market_cap(rec["ticker"])

    try:
        result = filters.evaluate(rec, body_provider, market_cap_provider)
    except Exception:
        log.exception("filter crashed on %s %s - recording as error",
                      rec.get("ticker"), doc_key)
        result = filters.Result(False, "filter_error", None, None, None, 0)

    store.record_announcement({
        "doc_key": doc_key,
        "ticker": rec["ticker"],
        "headline": rec["headline"],
        "published_utc": published.isoformat() if published else None,
        "published_syd": (to_sydney(published).isoformat()
                          if published else None),
        "market_state": state,
        "price_sensitive": int(bool(rec["price_sensitive"])),
        "pdf_url": feed.pdf_url(doc_key),
        "body_chars": result.body_chars,
        "body_unavailable": result.body_unavailable,
        "first_seen_utc": store.utcnow(),
    })

    store.record_filter(doc_key, rec["ticker"], rec["headline"],
                        "PASS" if result.passed else "REJECT",
                        result.reason, result.category, result.market_cap)

    if not result.passed:
        log.debug("reject %-4s %-22s %s", rec["ticker"], result.reason,
                  rec["headline"][:70])
        return "rejected"

    flag = " [PRICE-SENSITIVE]" if rec["price_sensitive"] else ""
    log.info("PASS %s%s  %s  %s", rec["ticker"], flag, result.category,
             rec["headline"][:80])
    shadowbook.record_decision(rec, result)
    return "passed"


def poll_once():
    """One sweep of the feed. Returns a counts dict; never raises."""
    counts = {"items": 0, "seen": 0, "skipped": 0, "rejected": 0,
              "passed": 0, "pages_failed": 0}

    from . import config
    for page in range(config.PAGES_PER_POLL):
        items = feed.fetch_page(page)
        if items is None:
            counts["pages_failed"] += 1
            break
        if not items:
            break

        counts["items"] += len(items)
        page_new = 0
        for item in items:
            try:
                outcome = process_item(item)
            except Exception:
                log.exception("item processing failed, continuing")
                outcome = "skipped"
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome != "seen":
                page_new += 1

        # The feed is newest-first. A page with nothing new on it means every
        # deeper page is older still, so there is no reason to keep paging.
        if page_new == 0:
            break
        if len(items) < config.ITEMS_PER_PAGE:
            break

    return counts
