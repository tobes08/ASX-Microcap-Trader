"""
Sprint 0.2.2 - 0.2.6: the historical event study and its gate.

Replays the harvested corpus through the SAME pre-filter, the SAME entry
rules, the SAME slippage tiers and the SAME benchmark as the forward shadow
book, so that the two can be compared like for like:

    python -m openclaw.eventstudy                  # whole corpus
    python -m openclaw.eventstudy --start 2025-11-01 --end 2026-10-31
    python -m openclaw.eventstudy --no-dedupe      # count per document

The question it answers is the one the whole project hangs on: do material
announcements on micro caps move the price by more than it costs to trade
them? The 0.2.6 gate is N >= 30 and a positive mean excess return over the
Small Ordinaries after slippage, per category and horizon.

Differences from the live book, all deliberate and all stated in the report:

  * Market cap is reconstructed as at the announcement (see histprices),
    because the live "current market cap" lookup cannot be run in the past.
  * The PDF body-length gate is not applied. It would cost one download per
    candidate, and it only removes cover-page-only notices.
  * Next to the book-equivalent numbers there is a cost stress test: a floor
    of half a tick per side, and no fill on a session that never traded.

It reads the research database and Yahoo. It never opens the live ledger.

System-design exercise, not financial advice.
"""
import argparse
import csv
import datetime as _dt
import json
import math
import os
import random
import statistics
import sys
from collections import Counter, defaultdict

from . import config, log as oclog

# Before anything imports the live modules: keep research logging out of the
# poller's rotating log file.
oclog.setup(filename="research.log")

from . import filters, histprices, histstore  # noqa: E402
from .histprices import CLOSE, DATE, OPEN, VOLUME  # noqa: E402
from .marketstate import SYDNEY, entry_plan  # noqa: E402

log = oclog.get("eventstudy")

# Same-day exit at the close, for entries taken at the open. Not a horizon
# the forward book tracks; reported separately as a diagnostic.
SAME_DAY = 0

# Stands in for the PDF text so the body-length gate passes.
_BODY_STUB = ("x" * (config.MIN_BODY_CHARS + 1), None)

CAP_BANDS = [
    (10_000_000, "under $10m"),
    (25_000_000, "$10m - $25m"),
    (50_000_000, "$25m - $50m"),
    (100_000_000, "$50m - $100m"),
]


# ============================================================ simulation ===

def stress_cost(haircut, quoted_price):
    """Per-side cost under the stress test: the tier haircut, floored at
    half a tick. Crossing a one-tick spread in and back out costs a full tick
    however lucky the timing, and on a sub-2c stock that alone is more than
    the haircut."""
    if not quoted_price or quoted_price <= 0:
        return haircut
    return max(haircut, 0.5 * histprices.tick_size(quoted_price) / quoted_price)


def simulate(series, bench, published_utc, haircut, horizons=None):
    """One virtual trade, by the shadow book's rules.

    Returns a dict. With no usable entry bar it carries only void_reason,
    exactly as the book records a VOID rather than a loss.
    """
    horizons = horizons or config.HORIZONS
    target, field_name = entry_plan(published_utc)
    if target is None:
        return {"void_reason": "bad_timestamp"}
    field = OPEN if field_name == "open" else CLOSE

    i = series.index_on_or_after(target)
    if i is None or ((series.dates[i] - target).days
                     > config.ENTRY_RESOLUTION_MAX_DAYS):
        return {"void_reason": "no_entry_bar"}

    entry_day = series.dates[i]
    entry_raw = series.adjusted(i, field)
    entry_quoted = series.quoted(i, field)
    entry_net = entry_raw * (1.0 + haircut)
    stress_entry_net = entry_raw * (1.0 + stress_cost(haircut, entry_quoted))
    volume = series.bars[i][VOLUME]

    prev = series.index_before(entry_day)
    gap_pct = None
    if prev is not None:
        gap_pct = (entry_raw / series.adjusted(prev, CLOSE) - 1.0) * 100.0

    bench_entry = _bench_close_on_or_after(bench, entry_day)

    out = {
        "void_reason": None,
        "entry_target": target, "entry_field": field_name,
        "entry_day": entry_day, "entry_price": entry_raw,
        "entry_quoted": entry_quoted, "entry_volume": volume,
        # adjusted close x adjusted volume: Yahoo restates volume for splits
        "entry_turnover": volume * series.bars[i][CLOSE],
        "prior_turnover": series.median_turnover_before(entry_day),
        "gap_pct": gap_pct, "haircut": haircut,
        "stress_fillable": volume > 0,
        "legs": {},
    }

    wanted = list(horizons)
    if field == OPEN:
        wanted = [SAME_DAY] + wanted
    for h in wanted:
        j = i + h
        if j >= len(series):
            continue
        exit_day = series.dates[j]
        exit_raw = series.adjusted(j, CLOSE)
        exit_quoted = series.quoted(j, CLOSE)
        ret = (exit_raw * (1.0 - haircut) / entry_net - 1.0) * 100.0
        stress_ret = (exit_raw * (1.0 - stress_cost(haircut, exit_quoted))
                      / stress_entry_net - 1.0) * 100.0

        bench_ret = None
        if h != SAME_DAY and bench_entry:
            bench_exit = _bench_close_on_or_after(bench, exit_day)
            if bench_exit:
                bench_ret = (bench_exit / bench_entry - 1.0) * 100.0

        gross = (exit_raw / entry_raw - 1.0) * 100.0
        out["legs"][h] = {
            "exit_day": exit_day, "ret": ret, "bench_ret": bench_ret,
            "gross": gross,
            "gross_excess": None if bench_ret is None else gross - bench_ret,
            "excess": None if bench_ret is None else ret - bench_ret,
            "stress_ret": stress_ret,
            "stress_excess": (None if bench_ret is None
                              else stress_ret - bench_ret),
            "suspect": series.discontinuity_between(
                entry_day, exit_day),
        }
    return out


def _bench_close_on_or_after(bench, day):
    if bench is None:
        return None
    i = bench.index_on_or_after(day)
    return None if i is None else bench.adjusted(i, CLOSE)


# ================================================================ events ===

def build_events(rows, prices, bench):
    """Run every corpus row through the live filter and the simulation.

    Every row comes back as an event dict, passed or not, so the funnel and
    the by-native-type view can see what the filter turned away.
    """
    events = []
    for row in rows:
        ticker = row["ticker"] or ""
        published = _dt.datetime.fromisoformat(row["published_utc"])
        syd_date = _dt.date.fromisoformat(row["syd_date"])
        series = prices.get(ticker)

        mcap = series.market_cap_before(syd_date) if series else None

        rec = {"ticker": ticker, "headline": row["headline"] or "",
               "price_sensitive": bool(row["price_sensitive"])}
        result = filters.evaluate(rec, lambda: _BODY_STUB, lambda: mcap)

        ev = {
            "doc_key": row["doc_key"], "ann_id": row["ann_id"],
            "ticker": ticker, "headline": row["headline"] or "",
            "published_utc": published, "syd_date": syd_date,
            "market_state": row["market_state"],
            "native_types": _json_list(row["native_types"]),
            "passed": result.passed, "reason": result.reason,
            "category": result.category, "mcap": mcap,
            "priced": series is not None,
            "in_universe": (mcap is not None
                            and mcap <= config.MAX_MARKET_CAP
                            and filters._TICKER_RE.match(ticker) is not None),
            "sim": None,
        }
        if series is not None and ev["in_universe"]:
            ev["sim"] = simulate(series, bench, published,
                                 filters.slippage_haircut(mcap))
        events.append(ev)
    return events


def _json_list(text):
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def dedupe(events, key):
    """Keep the first event per key. Two documents lodged for the same
    company on the same morning are one trading decision, and counting them
    twice inflates N and every t-statistic built on it."""
    seen = set()
    out = []
    for ev in sorted(events, key=lambda e: (e["published_utc"], e["doc_key"])):
        k = key(ev)
        if k in seen:
            continue
        seen.add(k)
        out.append(ev)
    return out


# ================================================================= stats ===

def _mean(xs):
    return sum(xs) / len(xs)


def tstat(xs):
    if len(xs) < 2:
        return None
    sd = statistics.stdev(xs)
    if sd == 0:
        return None
    return _mean(xs) / (sd / math.sqrt(len(xs)))


def clustered_tstat(pairs):
    """t-statistic of the pooled mean with errors clustered on a key.

    Announcements that share an entry date share that day's market, so they
    are not independent draws. Clustering by entry date stops a single good
    or bad day being counted as dozens of confirmations.
    """
    n = len(pairs)
    if n < 2:
        return None
    mean = sum(x for _, x in pairs) / n
    sums = defaultdict(float)
    for k, x in pairs:
        sums[k] += x - mean
    d = len(sums)
    if d < 2:
        return None
    var = d / (d - 1) * sum(s * s for s in sums.values()) / (n * n)
    if var <= 0:
        return None
    return mean / math.sqrt(var)


def winsorised_mean(xs, tail=0.05):
    """Mean with each tail clipped, to show how much of the plain mean is a
    handful of outliers."""
    s = sorted(xs)
    k = int(len(s) * tail)
    if k == 0:
        return _mean(s)
    lo, hi = s[k], s[len(s) - k - 1]
    return _mean([min(max(x, lo), hi) for x in s])


def legs_for(events, horizon):
    """Usable legs at one horizon, with the entry date kept for clustering."""
    out = []
    for ev in events:
        sim = ev["sim"]
        if not sim or sim["void_reason"]:
            continue
        leg = sim["legs"].get(horizon)
        if leg is None or leg["suspect"]:
            continue
        out.append((ev, sim, leg))
    return out


def cell(events, horizon, midpoint=None):
    """Statistics for one group of events at one horizon. None if empty."""
    legs = legs_for(events, horizon)
    if not legs:
        return None
    use_excess = horizon != SAME_DAY
    value = (lambda leg: leg["excess"]) if use_excess else (
        lambda leg: leg["ret"])

    used = [(sim, leg) for _, sim, leg in legs if value(leg) is not None]
    if not used:
        return None
    rets = [leg["ret"] for _, leg in used]
    scored = [(sim["entry_day"], value(leg)) for sim, leg in used]
    xs = [x for _, x in scored]

    gross = [leg["gross"] for _, leg in used]
    bench = [(leg["bench_ret"] if use_excess else 0.0) for _, leg in used]
    gross_x = [g - b for g, b in zip(gross, bench)]
    gross_scored = [(sim["entry_day"], gx)
                    for (sim, _), gx in zip(used, gross_x)]

    # The uniform per-side cost h at which the mean excess return is exactly
    # zero. With net = (1 + g) * (1 - h) / (1 + h) - 1 for each trade, mean
    # excess = 0 solves to (1 - h) / (1 + h) = (1 + mean bench) / mean(1 + g).
    # Zero or negative means the move does not even beat the benchmark
    # before any cost is paid.
    ratio = ((1.0 + _mean(bench) / 100.0)
             / _mean([1.0 + g / 100.0 for g in gross]))
    break_even = (1.0 - ratio) / (1.0 + ratio)

    stress = [(leg["stress_excess"] if use_excess else leg["stress_ret"])
              for sim, leg in used if sim["stress_fillable"]]
    stress = [x for x in stress if x is not None]

    out = {
        "n": len(xs),
        "mean_ret": _mean(rets), "median_ret": statistics.median(rets),
        "win": 100.0 * sum(1 for r in rets if r > 0) / len(rets),
        "mean": _mean(xs), "median": statistics.median(xs),
        "wmean": winsorised_mean(xs),
        "gross": _mean(gross_x), "gross_median": statistics.median(gross_x),
        "wgross": winsorised_mean(gross_x),
        "t_gross": clustered_tstat(gross_scored),
        "be": break_even,
        "cost": _mean([sim["haircut"] for sim, _ in used]),
        "t": tstat(xs), "t_cluster": clustered_tstat(scored),
        "stress_n": len(stress),
        "stress_mean": _mean(stress) if stress else None,
        "h1": None, "h2": None,
    }
    if midpoint is not None:
        first = [x for d, x in scored if d < midpoint]
        second = [x for d, x in scored if d >= midpoint]
        out["h1"] = _mean(first) if first else None
        out["h2"] = _mean(second) if second else None
    return out


def gate(stats):
    """The 0.2.6 gate exactly as the sprint plan words it."""
    if stats is None or stats["n"] < config.GATE_MIN_N:
        return "THIN"
    return "PASS" if stats["mean"] > 0 else "FAIL"


def robust(stats):
    """A stricter reading than the plan's gate, for the cells that pass it:
    the typical trade wins (median), the result is not one lucky day
    (clustered t >= 2), it held in both halves of the sample, and it
    survives the cost stress test."""
    if stats is None:
        return False
    checks = [
        stats["mean"] > 0,
        stats["median"] > 0,
        stats["t_cluster"] is not None and stats["t_cluster"] >= 2.0,
        stats["h1"] is not None and stats["h1"] > 0,
        stats["h2"] is not None and stats["h2"] > 0,
        stats["stress_mean"] is not None and stats["stress_mean"] > 0,
    ]
    return all(checks)


# ================================================================ report ===

def _f(x, width=8, digits=2, sign=True):
    if x is None:
        return " " * (width - 3) + "n/a"
    return f"{x:>{'+' if sign else ''}{width}.{digits}f}"


def _bar(title):
    return f"\n{title}\n" + "-" * len(title)


def _be_text(be, width=6):
    """Break-even per-side cost in percent. '<0' means the gross move does
    not beat the benchmark before any cost."""
    if be is None:
        return " " * (width - 3) + "n/a"
    if be <= 0:
        return f"{'<0':>{width}}"
    return f"{100.0 * be:>{width}.1f}"


_HDR = (f"  {'group':<40}{'hor':>5}{'n':>6}{'win%':>7}"
        f"{'excess':>8}{'median':>8}{'gross':>8}{'w.gross':>8}{'t(g)':>6}{'BE%':>6}"
        f"{'t(cl)':>7}{'stress':>8}{'H1':>7}{'H2':>7}  gate")


def _row(label, horizon, s):
    hor = "t+0" if horizon == SAME_DAY else f"t+{horizon}"
    verdict = gate(s)
    if verdict == "PASS" and robust(s):
        verdict = "PASS*"
    return (f"  {label[:39]:<40}{hor:>5}{s['n']:>6}{s['win']:>7.1f}"
            f"{_f(s['mean'])}{_f(s['median'])}"
            f"{_f(s['gross'])}{_f(s['wgross'])}{_f(s['t_gross'], 6, 1)}"
            f"{_be_text(s['be'])}"
            f"{_f(s['t_cluster'], 7, 1)}"
            f"{_f(s['stress_mean'])}{_f(s['h1'], 7, 1)}{_f(s['h2'], 7, 1)}"
            f"  {verdict}")


def _table(groups, horizons, midpoint, min_n=None):
    """groups: ordered list of (label, events)."""
    min_n = config.STUDY_MIN_N_TO_REPORT if min_n is None else min_n
    lines = [_HDR]
    shown = 0
    for label, evs in groups:
        block = []
        for h in horizons:
            s = cell(evs, h, midpoint)
            if s is None or s["n"] < min_n:
                continue
            block.append(_row(label, h, s))
        if block:
            lines.extend(block)
            shown += 1
    if not shown:
        lines.append("  (no group reaches the minimum N to report)")
    return lines


def _legend():
    return [
        "",
        "  win%    share of trades with a positive net return",
        "  excess  mean net return (after the haircut on both sides) minus "
        "the benchmark over the same days",
        "  median  median net excess",
        "  gross   mean excess BEFORE any slippage: what the price did, "
        "ignoring what it costs to trade",
        "  w.gross the same with the top and bottom 5% of trades clipped: "
        "what the typical trade did,",
        "          without a few moonshots or blow-ups setting the mean",
        "  t(g)    t-stat of mean GROSS excess, clustered by entry date: is "
        "there a price reaction at all,",
        "          before costs? Beyond +-2 is worth a look, and with this "
        "many cells some will get there by luck.",
        "  BE%     the uniform per-side cost that would bring mean excess to "
        "zero. Compare it with the 3.0% / 1.5%",
        "          the book assumes. '<0' means the move does not beat the "
        "benchmark even for free.",
        "  t(cl)   t-stat of mean net excess, clustered by entry date",
        "  stress  mean net excess with a half-tick cost floor per side and "
        "no fill on",
        "          sessions that never traded",
        "  H1 H2   mean net excess in the first and second half of the "
        "sample",
        f"  gate    PASS = N >= {config.GATE_MIN_N} and net excess > 0 (the "
        "plan's 0.2.6 gate).  THIN = N too small.",
        "          PASS* = also positive median, t(cl) >= 2, positive in "
        "both halves and under stress.",
    ]


def _cap_band(mcap):
    for ceiling, label in CAP_BANDS:
        if mcap < ceiling:
            return label
    return None


def placebo_trades(real, prices, bench, start, end, draws=None, seed=None):
    """The control experiment: the same stocks on other mornings.

    For each real trade, `draws` virtual trades in the same company on
    pre-open mornings between EXCLUDE and WINDOW calendar days away from its
    announcement (see config), entered at that morning's open under exactly
    the same rules, costs and size screen. Returns {real doc_key: [placebo
    event, ...]}.

    Without this a negative result is ambiguous: it could be the
    announcements, or it could be that sub-$100m stocks simply lag the Small
    Ordinaries on any given morning. Drawing from the same season keeps
    reporting-season swings out of the comparison, and the exclusion gap keeps
    each placebo's holding window clear of the real reaction.
    """
    draws = draws or config.STUDY_PLACEBO_DRAWS
    rng = random.Random(config.STUDY_PLACEBO_SEED if seed is None else seed)
    exclude = _dt.timedelta(days=config.STUDY_PLACEBO_EXCLUDE_DAYS)
    window = _dt.timedelta(days=config.STUDY_PLACEBO_WINDOW_DAYS)

    real_days = defaultdict(list)
    for e in real:
        real_days[e["ticker"]].append(e["syd_date"])

    out = {}
    for e in real:
        series = prices.get(e["ticker"])
        if series is None:
            continue
        lo = series.index_on_or_after(max(start, e["syd_date"] - window))
        # last session ON OR BEFORE the far edge: rolling forward from a
        # weekend would step outside the window
        hi = series.index_before(min(end, e["syd_date"] + window)
                                 + _dt.timedelta(days=1))
        if lo is None or hi is None or hi <= lo:
            continue
        candidates = [
            i for i in range(lo, hi + 1)
            if all(abs(series.dates[i] - d) > exclude
                   for d in real_days[e["ticker"]])]
        rng.shuffle(candidates)

        found = []
        for i in candidates:
            if len(found) >= draws:
                break
            day = series.dates[i]
            mcap = series.market_cap_before(day)
            if mcap is None or mcap > config.MAX_MARKET_CAP:
                continue
            published = _dt.datetime.combine(
                day, _dt.time(8, 0), tzinfo=SYDNEY).astimezone(_dt.timezone.utc)
            sim = simulate(series, bench, published,
                           filters.slippage_haircut(mcap))
            if sim["void_reason"]:
                continue
            found.append({"ticker": e["ticker"], "syd_date": day,
                          "mcap": mcap, "sim": sim})
        if found:
            out[e["doc_key"]] = found
    return out


def _gross_excess(ev, horizon):
    sim = ev["sim"]
    if not sim or sim["void_reason"]:
        return None
    leg = sim["legs"].get(horizon)
    if leg is None or leg["suspect"]:
        return None
    return leg["gross_excess"]


def announcement_effect(real, placebo, horizon):
    """Paired difference in gross excess: each real trade minus the mean of
    its own placebos. Returns (n, mean difference, clustered t) or None."""
    pairs = []
    for e in real:
        mine = _gross_excess(e, horizon)
        theirs = [g for g in (_gross_excess(p, horizon)
                              for p in placebo.get(e["doc_key"], ()))
                  if g is not None]
        if mine is None or not theirs:
            continue
        pairs.append((e["sim"]["entry_day"], mine - _mean(theirs)))
    if not pairs:
        return None
    return (len(pairs), _mean([d for _, d in pairs]),
            clustered_tstat(pairs))


def _liq_band(turnover):
    if turnover is None:
        return "unknown"
    for ceiling, label in config.LIQUIDITY_BANDS:
        if turnover < ceiling:
            return label
    return "unknown"


def _readout(summary_groups, category_groups, horizons, midpoint):
    cells = []
    for label, evs in category_groups:
        for h in horizons:
            s = cell(evs, h, midpoint)
            cells.append((label, h, gate(s), robust(s)))
    passes = [(c, h) for c, h, g, _ in cells if g == "PASS"]
    strong = [(c, h) for c, h, g, r in cells if g == "PASS" and r]

    lines = [_bar("READOUT")]
    lines.append(f"  Plan gate (N >= {config.GATE_MIN_N}, net excess > 0): "
                 f"{len(passes)} of {len(cells)} category cells pass, "
                 f"{len(strong)} pass every robustness check (PASS*).")
    lines.append("  Mean excess over the benchmark BEFORE any cost, its "
                 "significance t, and the uniform")
    lines.append("  per-side cost (BE) that would erase it, against what the "
                 "book assumes:")
    for label, evs in summary_groups:
        parts, cost = [], None
        for h in horizons:
            s = cell(evs, h, midpoint)
            if s is None:
                continue
            cost = s["cost"]
            tg = s["t_gross"]
            parts.append(f"t+{h} {s['gross']:+.2f}% (t "
                         f"{'n/a' if tg is None else format(tg, '+.1f')}, "
                         f"BE {_be_text(s['be']).strip()}%)")
        if parts:
            lines.append(f"    {label:<15}" + "   ".join(parts)
                         + (f"   assumed {100 * cost:.1f}%/side"
                            if cost is not None else ""))
    return lines, cells, passes, strong


def build_report(events, meta, dedupe_events=True, prices=None,
                 bench=None):
    horizons = list(config.HORIZONS)
    start, end = meta["start"], meta["end"]
    midpoint = start + (end - start) / 2
    out = []
    add = out.append

    add("=" * 100)
    add(f"OpenClaw event study   |   {start} .. {end}   |   generated "
        f"{histstore.utcnow()}")
    add(f"universe: market cap <= ${config.MAX_MARKET_CAP / 1e6:.0f}m at the "
        f"announcement   benchmark: {meta['benchmark']}   horizons: "
        f"{horizons}")
    add("=" * 100)

    # ------------------------------------------------------------ corpus ---
    add(_bar("CORPUS AND DATA QUALITY"))
    tickers = {e["ticker"] for e in events}
    unpriced = {e["ticker"] for e in events if not e["priced"]}
    add(f"  Price-sensitive announcements: {len(events):,} from "
        f"{len(tickers):,} tickers")
    add(f"  Tickers with no price history at Yahoo: {len(unpriced):,} "
        f"({sum(1 for e in events if not e['priced']):,} announcements). "
        "Delisted, renamed or never covered.")
    if meta.get("price_errors"):
        add(f"  WARNING: {meta['price_errors']} tickers failed to download. "
            "Rerun to fill them in before trusting the counts.")
    no_cap = sum(1 for e in events if e["priced"] and e["mcap"] is None)
    add(f"  Priced but market cap not reconstructable (no share history or "
        f"no prior bar): {no_cap:,}")
    add("  Survivorship: every company above still trades today. Names that "
        "were taken over or went")
    add("  under since are missing, and those two groups do not cancel.")

    # ------------------------------------------------------------ funnel ---
    add(_bar("PRE-FILTER FUNNEL (the live filter, replayed)"))
    total = len(events)
    passed = [e for e in events if e["passed"]]
    add(f"  Evaluated {total:,}, passed {len(passed):,} "
        f"({100.0 * len(passed) / max(total, 1):.1f}%)")
    add("  Rejection reasons:")
    for reason, n in Counter(e["reason"] for e in events
                             if not e["passed"]).most_common():
        add(f"    {reason:<24} {n:>7,}  ({100.0 * n / total:.1f}%)")
    digit = sum(1 for e in passed if any(c.isdigit() for c in e["ticker"]))
    add(f"  Passing announcements from codes containing a digit: {digit:,} "
        "(the letters-only ticker gate rejected all of these)")
    add("  The PDF body-length gate is not applied here, so this is a "
        "slight superset of the live book.")

    traded = [e for e in passed if e["sim"]]
    voids = Counter(e["sim"]["void_reason"] for e in traded
                    if e["sim"]["void_reason"])
    live = [e for e in traded if not e["sim"]["void_reason"]]
    if dedupe_events:
        before = len(live)
        live = dedupe(live, lambda e: (e["ticker"], e["sim"]["entry_day"],
                                       e["category"]))
        add(f"  Virtual trades: {before:,} documents -> {len(live):,} after "
            "merging same company, same entry session, same category")
    else:
        add(f"  Virtual trades: {len(live):,} (one per document, as the "
            "forward book counts them)")
    for reason, n in voids.most_common():
        add(f"  void: {reason:<20}{n:>6,}")
    suspect = sum(1 for e in live for leg in e["sim"]["legs"].values()
                  if leg["suspect"])
    if suspect:
        add(f"  Legs excluded because a split or consolidation was never "
            f"applied to the price history: {suspect:,}")
    zero = sum(1 for e in live if not e["sim"]["stress_fillable"])
    add(f"  Entries on a session with zero volume: {zero:,} "
        f"({100.0 * zero / max(len(live), 1):.1f}%). The book fills these "
        "at the stale price; the stress test does not.")
    floor = sum(1 for e in live if stress_cost(
        e["sim"]["haircut"], e["sim"]["entry_quoted"]) > e["sim"]["haircut"])
    add(f"  Entries where half a tick exceeds the haircut: {floor:,} "
        f"({100.0 * floor / max(len(live), 1):.1f}%)")
    if live:
        turn = sorted(e["sim"]["entry_turnover"] for e in live)
        add(f"  Turnover in the entry session itself: median "
            f"${turn[len(turn) // 2]:,.0f}")
    known = [e for e in live if e["sim"]["prior_turnover"] is not None]
    if known:
        usual = sorted(e["sim"]["prior_turnover"] for e in known)
        big = sum(1 for e in known
                  if config.NOTIONAL_PER_TRADE > 0.10 * e["sim"]["prior_turnover"])
        add(f"  Usual daily turnover (median of the prior "
            f"{config.LIQUIDITY_LOOKBACK_SESSIONS} sessions): median "
            f"${usual[len(usual) // 2]:,.0f}. A "
            f"${config.NOTIONAL_PER_TRADE:,.0f} position is more than 10% "
            f"of it on {big:,} of {len(known):,} trades "
            f"({100.0 * big / len(known):.1f}%).")

    # --------------------------------------------------- group definitions ---
    # One trade per company per session for the aggregate rows: two documents
    # in different categories on the same morning are one price move, and
    # counting it twice would inflate N.
    same_move = (lambda e: (e["ticker"], e["sim"]["entry_day"]))
    if dedupe_events:
        unique = dedupe(live, same_move)
        ex_earn = dedupe([e for e in live if e["category"] != "EARNINGS"],
                         same_move)
    else:
        unique = live
        ex_earn = [e for e in live if e["category"] != "EARNINGS"]
    by_cat = defaultdict(list)
    for e in live:
        by_cat[e["category"]].append(e)
    category_groups = sorted(by_cat.items(), key=lambda kv: -len(kv[1]))
    summary_groups = [("ALL TRADEABLE", unique),
                      ("EX-EARNINGS", ex_earn)]
    earn_share = 100.0 * len(by_cat.get("EARNINGS", [])) / max(len(live), 1)
    add(f"  EARNINGS (mostly scheduled quarterly 4C/5B filings) is "
        f"{earn_share:.0f}% of all virtual trades.")

    readout, cells, passes, strong = _readout(
        summary_groups, category_groups, horizons, midpoint)
    out.extend(readout)

    # -------------------------------------------------------------- gate ---
    add(_bar("0.2.6 GATE: BY CATEGORY, AS THE LIVE FILTER TRADES THEM"))
    out.extend(_table(summary_groups + category_groups, horizons, midpoint,
                      min_n=1))
    out.extend(_legend())
    add("")
    add(f"  Cells passing the plan's gate: {len(passes)} of {len(cells)}"
        + (": " + ", ".join(f"{c} t+{h}" for c, h in passes)
           if passes else ""))
    add(f"  Of those, passing every robustness check: {len(strong)}"
        + (": " + ", ".join(f"{c} t+{h}" for c, h in strong)
           if strong else ""))
    add(f"  With {len(cells)} cells tested, one or two would clear the "
        "plan's gate by luck alone. PASS* is the bar to trust.")

    # ----------------------------------------------------------- placebo ---
    if prices is not None:
        add(_bar("CONTROL: THE SAME STOCKS ON RANDOM MORNINGS"))
        add(f"  Pre-open announcements only (entry at the open), against "
            f"{config.STUDY_PLACEBO_DRAWS} placebo trades per real one: the "
            "same company on other")
        add(f"  mornings {config.STUDY_PLACEBO_EXCLUDE_DAYS} to "
            f"{config.STUDY_PLACEBO_WINDOW_DAYS} days either side of its "
            "announcement (same season, holding windows clear of it),")
        add("  same size screen, costs and benchmark.")
        add("  If real and placebo look alike, the announcement adds "
            "nothing: it is just how these stocks")
        add("  behave against the benchmark. The effect is real minus "
            "placebo, paired per trade, before costs.")
        pre = [e for e in unique if e["market_state"] == "PRE_OPEN"]
        placebo = placebo_trades(pre, prices, bench, start, end)
        pre_cats = defaultdict(list)
        for e in pre:
            pre_cats[e["category"]].append(e)
        control_groups = [
            ("ALL TRADEABLE", pre),
            ("EX-EARNINGS", [e for e in pre if e["category"] != "EARNINGS"]),
        ] + [(c, pre_cats[c]) for c, _ in category_groups
             if c != "EARNINGS" and len(pre_cats.get(c, ())) >= config.GATE_MIN_N]
        rows = []
        effects = []
        for label, evs in control_groups:
            fake = [p for e in evs for p in placebo.get(e["doc_key"], ())]
            rows.append((f"{label} (real)", evs))
            rows.append((f"{label} (placebo)", fake))
            effects.append((label, evs))
        out.extend(_table(rows, horizons, midpoint, min_n=config.GATE_MIN_N))
        add("")
        add("  Announcement effect, gross (real minus placebo, mean excess "
            "points, clustered t):")
        add(f"    {'group':<22}" + "".join(
            f"{'t+' + str(h):>22}" for h in horizons))
        for label, evs in effects:
            cols = []
            for h in horizons:
                r = announcement_effect(evs, placebo, h)
                if r is None or r[0] < config.GATE_MIN_N:
                    cols.append(f"{'n/a':>22}")
                else:
                    n, d, t = r
                    cols.append(f"{d:>+9.2f} (t {t if t is not None else float('nan'):+5.1f}, n={n})")
            add(f"    {label:<22}" + "".join(cols))

    # --------------------------------------------------------- liquidity ---
    add(_bar("BY USUAL LIQUIDITY (known before the trade)"))
    add(f"  Median daily turnover over the {config.LIQUIDITY_LOOKBACK_SESSIONS}"
        " sessions before entry. Costs fall as liquidity rises, so if there "
        "is an edge,")
    add("  this is where it should show first. One trade per company per "
        "session.")
    order = [label for _, label in config.LIQUIDITY_BANDS] + ["unknown"]
    for title, pool in (("all tradeable", unique), ("ex-earnings", ex_earn)):
        by_liq = defaultdict(list)
        for e in pool:
            by_liq[_liq_band(e["sim"]["prior_turnover"])].append(e)
        add("")
        add(f"  {title}:  trades per band: " + "   ".join(
            f"{b} {len(by_liq[b]):,}" for b in order if by_liq.get(b)))
        out.extend(_table([(b, by_liq[b]) for b in order if by_liq.get(b)],
                          horizons, midpoint, min_n=config.GATE_MIN_N))

    # ------------------------------------------------------ market state ---
    add(_bar("BY MARKET STATE AT PUBLICATION (one trade per company per "
             "session)"))
    by_state = defaultdict(list)
    for e in unique:
        by_state[f"{e['market_state']} -> {e['sim']['entry_field']}"].append(e)
    out.extend(_table(sorted(by_state.items(), key=lambda kv: -len(kv[1])),
                      horizons, midpoint, min_n=config.GATE_MIN_N))

    # ---------------------------------------------------------- cap band ---
    add(_bar("BY MARKET CAP BAND (one trade per company per session)"))
    by_band = defaultdict(list)
    for e in unique:
        by_band[_cap_band(e["mcap"])].append(e)
    order = [label for _, label in CAP_BANDS]
    out.extend(_table([(b, by_band[b]) for b in order if by_band.get(b)],
                      horizons, midpoint, min_n=config.GATE_MIN_N))
    add("")
    add("  Trades per category and cap band (can each cell reach N of "
        f"{config.GATE_MIN_N}?):")
    add(f"    {'category':<22}" + "".join(f"{b:>15}" for b in order))
    for cat, evs in category_groups:
        counts = Counter(_cap_band(e["mcap"]) for e in evs)
        add(f"    {cat:<22}" + "".join(f"{counts.get(b, 0):>15,}"
                                       for b in order))

    # --------------------------------------------------------------- gap ---
    add(_bar("HOW MUCH HAS ALREADY MOVED BEFORE THE ASSUMED ENTRY"))
    add("  Move from the previous close to the entry price. This part of "
        "the reaction is not capturable")
    add("  by a daily-bar entry. Whether any of it is capturable earlier "
        "needs intraday data.")
    add(f"    {'category':<22}{'entry':>7}{'n':>7}{'mean gap':>10}"
        f"{'median':>9}{'gap > 5%':>10}")
    for cat, evs in category_groups:
        for fld in ("open", "close"):
            gaps = [e["sim"]["gap_pct"] for e in evs
                    if e["sim"]["entry_field"] == fld
                    and e["sim"]["gap_pct"] is not None]
            if len(gaps) < config.STUDY_MIN_N_TO_REPORT:
                continue
            add(f"    {cat:<22}{fld:>7}{len(gaps):>7,}{_f(_mean(gaps), 10)}"
                f"{_f(statistics.median(gaps), 9)}"
                f"{100.0 * sum(1 for g in gaps if g > 5) / len(gaps):>9.1f}%")

    # ---------------------------------------------------------- same day ---
    add(_bar("SAME-DAY DIAGNOSTIC: BUY THE OPEN, SELL THAT DAY'S CLOSE"))
    add("  Pre-open announcements only. Net return, no benchmark. Not a "
        "horizon the forward book tracks.")
    out.extend(_table(summary_groups + category_groups, [SAME_DAY], midpoint,
                      min_n=config.GATE_MIN_N))

    # --------------------------------------------------- inside category ---
    add(_bar("INSIDE EACH CATEGORY: BY NATIVE ASX ANNOUNCEMENT TYPE (t+5)"))
    add("  What each regex category is really made of. A document with "
        "several native types appears under each.")
    sub_groups = []
    for cat, evs in category_groups:
        by_native = defaultdict(list)
        for e in evs:
            for t in e["native_types"] or ["<none>"]:
                by_native[t].append(e)
        big = [(t, sub) for t, sub in sorted(
            by_native.items(), key=lambda kv: -len(kv[1]))
            if len(sub) >= config.GATE_MIN_N]
        sub_groups.extend((f"{cat[:11]} / {t}", sub) for t, sub in big[:6])
    out.extend(_table(sub_groups, [5], midpoint, min_n=config.GATE_MIN_N))
    add("  (the six most common native types per category)")

    # -------------------------------------------------------- native view ---
    add(_bar("EVERYTHING PRICE SENSITIVE UNDER THE CAP: BY NATIVE ASX TYPE "
             "(t+5)"))
    add("  Ignores the regex categories and headline exclusions, so it shows "
        "what the filter is missing")
    add("  as well as what it trades. One trade per company per session "
        "within each type.")
    universe = [e for e in events
                if e["in_universe"] and e["sim"]
                and not e["sim"]["void_reason"]]
    by_native = defaultdict(list)
    for e in universe:
        for t in e["native_types"] or ["<none>"]:
            by_native[t].append(e)
    native_groups = []
    for t, evs in by_native.items():
        if dedupe_events:
            evs = dedupe(evs, same_move)
        native_groups.append((t, evs))
    native_groups.sort(key=lambda kv: -len(kv[1]))
    out.extend(_table(native_groups[:40], [5], midpoint,
                      min_n=config.GATE_MIN_N))

    ranked = []
    for t, evs in native_groups:
        s = cell(evs, 5, midpoint)
        if s is not None and s["n"] >= config.GATE_MIN_N:
            ranked.append((s["gross"], t, evs))
    ranked.sort(key=lambda r: -r[0])
    add("")
    add(f"  Largest gross reactions at t+5 (N >= {config.GATE_MIN_N}). With "
        f"{len(ranked)} types ranked, the top few are partly luck:")
    out.extend(_table([(t, evs) for _, t, evs in ranked[:12]], [5], midpoint,
                      min_n=config.GATE_MIN_N))

    captured = Counter()
    for e in universe:
        for t in e["native_types"] or ["<none>"]:
            captured[(t, e["passed"])] += 1
    add("")
    add("  Share of each native type the live filter currently passes "
        "(25 most common types):")
    for t, evs in native_groups[:25]:
        n = captured[(t, True)] + captured[(t, False)]
        add(f"    {t[:44]:<45}{n:>6,}  passed "
            f"{100.0 * captured[(t, True)] / max(n, 1):5.1f}%")

    add("")
    add("System-design exercise, not financial advice. A backtest on daily "
        "bars cannot see the order book:")
    add("treat every entry price as approximate and lean pessimistic.")
    return "\n".join(out), live


# ================================================================ output ===

def write_events_csv(events, path, horizons=None):
    horizons = [SAME_DAY] + list(horizons or config.HORIZONS)
    cols = ["doc_key", "ticker", "published_utc", "syd_date", "market_state",
            "category", "native_types", "headline", "market_cap", "haircut",
            "entry_day", "entry_field", "entry_price", "entry_quoted",
            "entry_volume", "prior_turnover", "gap_pct"]
    for h in horizons:
        cols += [f"gross_t{h}", f"ret_t{h}", f"excess_t{h}",
                 f"stress_ret_t{h}"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for e in events:
            sim = e["sim"]
            row = [e["doc_key"], e["ticker"], e["published_utc"].isoformat(),
                   e["syd_date"].isoformat(), e["market_state"],
                   e["category"], "|".join(e["native_types"]), e["headline"],
                   round(e["mcap"]) if e["mcap"] else "", sim["haircut"],
                   sim["entry_day"].isoformat(), sim["entry_field"],
                   round(sim["entry_price"], 6),
                   round(sim["entry_quoted"], 6), sim["entry_volume"],
                   "" if sim["prior_turnover"] is None
                   else round(sim["prior_turnover"]),
                   "" if sim["gap_pct"] is None else round(sim["gap_pct"], 3)]
            for h in horizons:
                leg = sim["legs"].get(h)
                if leg is None or leg["suspect"]:
                    row += ["", "", "", ""]
                else:
                    row += [round(leg["gross"], 3), round(leg["ret"], 3),
                            "" if leg["excess"] is None
                            else round(leg["excess"], 3),
                            round(leg["stress_ret"], 3)]
            w.writerow(row)


def run(start=None, end=None, dedupe_events=True, refresh_prices=False,
        progress=None):
    """Run the study over the corpus. Returns (report_text, traded_events)."""
    progress = progress or (lambda msg: log.info("%s", msg))
    histstore.init()
    cov = histstore.coverage()
    if not cov["days"]:
        raise SystemExit("The corpus is empty. Run first:\n"
                         "    python -m openclaw.harvest --months 24 "
                         "--price-sensitive-only")
    start = start or _dt.date.fromisoformat(cov["first_day"])
    end = end or _dt.date.fromisoformat(cov["last_day"])

    rows = histstore.announcements(start, end, price_sensitive_only=True)
    progress(f"corpus: {len(rows):,} price-sensitive announcements "
             f"{start}..{end}")

    yesterday = _dt.datetime.now(SYDNEY).date() - _dt.timedelta(days=1)
    price_start = start - _dt.timedelta(
        days=config.STUDY_PRICE_PAD_BEFORE_DAYS)
    price_end = min(end + _dt.timedelta(days=max(config.HORIZONS) * 2 + 15),
                    yesterday)

    tickers = {r["ticker"] for r in rows
               if r["ticker"] and filters._TICKER_RE.match(r["ticker"])}
    prices, status = histprices.load_many(
        sorted(tickers), price_start, price_end, refresh=refresh_prices,
        progress=progress)

    bench, bench_symbol = None, None
    for sym in config.BENCHMARK_TICKERS:
        got, _ = histprices.load_many([sym], price_start, price_end,
                                      refresh=refresh_prices,
                                      progress=lambda m: None)
        if got.get(sym) is not None:
            bench, bench_symbol = got[sym], sym
            break
    if bench is None:
        progress("WARNING: no benchmark data, excess returns will be blank")

    events = build_events(rows, prices, bench)
    meta = {"start": start, "end": end, "benchmark": bench_symbol or "none",
            "price_errors": sum(1 for s in status.values() if s == "error")}
    text, traded = build_report(events, meta, dedupe_events,
                                prices=prices, bench=bench)

    os.makedirs(config.RESEARCH_DIR, exist_ok=True)
    report_path = os.path.join(config.RESEARCH_DIR, "eventstudy_report.txt")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    csv_path = os.path.join(config.RESEARCH_DIR, "eventstudy_trades.csv")
    write_events_csv(traded, csv_path)
    progress(f"report: {report_path}")
    progress(f"trades: {csv_path}")
    return text, traded


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=_dt.date.fromisoformat,
                    help="first Sydney date (default: start of the corpus)")
    ap.add_argument("--end", type=_dt.date.fromisoformat,
                    help="last Sydney date (default: end of the corpus)")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="count one trade per document, as the forward book "
                         "does, instead of one per company per session")
    ap.add_argument("--refresh-prices", action="store_true",
                    help="ignore the price cache and download again")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    text, _ = run(args.start, args.end, dedupe_events=not args.no_dedupe,
                  refresh_prices=args.refresh_prices)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
