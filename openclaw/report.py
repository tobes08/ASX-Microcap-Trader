"""
Ledger report.

Read-only. Safe to run while the poller is running - the database is in WAL
mode, so this never blocks the writer.

Reports P&L per horizon rather than one blended number, because the three
horizons are parallel hypotheticals on the same event, not a sequence of
positions. Adding them together would triple-count every trade.
"""
import argparse
import statistics
from collections import defaultdict

from . import config, store


def _pct(x, digits=2):
    return "n/a" if x is None else f"{x:+.{digits}f}%"


def _bar(title):
    return f"\n{title}\n" + "-" * len(title)


def filter_summary(conn):
    out = [_bar("PRE-FILTER")]
    total = conn.execute("SELECT COUNT(*) c FROM filter_log").fetchone()["c"]
    if not total:
        out.append("  No announcements evaluated yet.")
        return "\n".join(out)

    passed = conn.execute(
        "SELECT COUNT(*) c FROM filter_log WHERE decision='PASS'"
    ).fetchone()["c"]
    out.append(f"  Evaluated {total}, passed {passed} "
               f"({100.0 * passed / total:.1f}%)")

    out.append("  Rejection reasons:")
    rows = conn.execute(
        "SELECT reason, COUNT(*) c FROM filter_log WHERE decision='REJECT' "
        "GROUP BY reason ORDER BY c DESC").fetchall()
    for r in rows:
        out.append(f"    {r['reason']:<24} {r['c']:>6}  "
                   f"({100.0 * r['c'] / total:.1f}%)")

    unknown = next((r["c"] for r in rows if r["reason"] == "mcap_unknown"), 0)
    if unknown and unknown > 0.30 * total:
        out.append("")
        out.append("  WARNING: a large share of rejects are mcap_unknown. "
                   "That usually means")
        out.append("  the market-cap lookup is failing rather than the "
                   "universe being wrong.")

    flagged = conn.execute(
        "SELECT COUNT(*) c FROM announcements WHERE body_unavailable=1"
    ).fetchone()["c"]
    if flagged:
        out.append(f"\n  PDFs with no extractable text (passed with flag): "
                   f"{flagged}")
    return "\n".join(out)


def book_summary(conn):
    out = [_bar("SHADOW BOOK")]
    counts = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM trades GROUP BY status")}
    if not counts:
        out.append("  No virtual trades yet.")
        return "\n".join(out)
    out.append("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    bench = conn.execute(
        "SELECT benchmark_ticker t, COUNT(*) c FROM trades "
        "WHERE benchmark_ticker IS NOT NULL GROUP BY t ORDER BY c DESC"
    ).fetchone()
    if bench:
        out.append(f"  Benchmark: {bench['t']}")

    voids = conn.execute(
        "SELECT void_reason r, COUNT(*) c FROM trades WHERE status='VOID' "
        "GROUP BY r ORDER BY c DESC").fetchall()
    for v in voids:
        out.append(f"  void: {v['r'] or 'unknown':<24}{v['c']:>4}")
    return "\n".join(out)


def horizon_summary(conn):
    out = [_bar("REALISED P&L BY HORIZON")]
    rows = conn.execute(
        "SELECT l.horizon, l.ret_pct, l.pnl_aud, l.excess_pct, t.category "
        "FROM trade_legs l JOIN trades t ON t.id = l.trade_id").fetchall()
    if not rows:
        out.append("  Nothing has reached an exit horizon yet.")
        return "\n".join(out)

    by_h = defaultdict(list)
    for r in rows:
        by_h[r["horizon"]].append(r)

    hdr = (f"  {'horizon':<9}{'n':>5}{'hit rate':>11}{'mean':>10}"
           f"{'median':>10}{'total $':>12}{'mean excess':>14}")
    out.append(hdr)
    for h in sorted(by_h):
        legs = by_h[h]
        rets = [x["ret_pct"] for x in legs if x["ret_pct"] is not None]
        pnls = [x["pnl_aud"] for x in legs if x["pnl_aud"] is not None]
        exc = [x["excess_pct"] for x in legs if x["excess_pct"] is not None]
        if not rets:
            continue
        wins = sum(1 for x in rets if x > 0)
        excess_col = f"{statistics.mean(exc):+.2f}" if exc else "n/a"
        out.append(
            f"  t+{h:<7}{len(rets):>5}{100.0 * wins / len(rets):>10.1f}%"
            f"{statistics.mean(rets):>+10.2f}"
            f"{statistics.median(rets):>+10.2f}"
            f"{sum(pnls):>+12.2f}{excess_col:>14}")

    out.append("\n  Horizons are parallel hypotheticals on the same events. "
               "Do not add them up.")
    return "\n".join(out)


def category_summary(conn):
    out = [_bar("BY CATEGORY")]
    rows = conn.execute(
        "SELECT t.category, l.horizon, l.ret_pct, l.excess_pct "
        "FROM trade_legs l JOIN trades t ON t.id = l.trade_id "
        "WHERE l.ret_pct IS NOT NULL").fetchall()
    if not rows:
        out.append("  Nothing closed yet.")
        return "\n".join(out)

    buckets = defaultdict(list)
    for r in rows:
        buckets[(r["category"], r["horizon"])].append(r)

    out.append(f"  {'category':<22}{'horizon':>8}{'n':>5}{'mean':>10}"
               f"{'hit':>8}{'mean excess':>14}")
    for (cat, h) in sorted(buckets, key=lambda k: (k[0] or "", k[1])):
        legs = buckets[(cat, h)]
        rets = [x["ret_pct"] for x in legs]
        exc = [x["excess_pct"] for x in legs if x["excess_pct"] is not None]
        wins = sum(1 for x in rets if x > 0)
        excess_col = f"{statistics.mean(exc):+.2f}" if exc else "n/a"
        out.append(f"  {(cat or '?'):<22}{('t+' + str(h)):>8}{len(rets):>5}"
                   f"{statistics.mean(rets):>+10.2f}"
                   f"{100.0 * wins / len(rets):>7.0f}%{excess_col:>14}")

    small = sum(1 for k in buckets if len(buckets[k]) < 10)
    if small:
        out.append(f"\n  {small} of {len(buckets)} cells have fewer than 10 "
                   "observations. Treat as anecdote, not evidence.")
    return "\n".join(out)


def open_positions(conn):
    out = [_bar("OPEN POSITIONS")]
    rows = conn.execute(
        "SELECT * FROM trades WHERE status IN ('OPEN','PENDING_ENTRY') "
        "ORDER BY decision_utc").fetchall()
    if not rows:
        out.append("  None.")
        return "\n".join(out)
    out.append(f"  {'ticker':<8}{'category':<20}{'status':<15}{'entry':<12}"
               f"{'entry px':>10}{'unreal':>10}  headline")
    for r in rows:
        entry_px = ("%10.4f" % r["entry_price_net"]
                    if r["entry_price_net"] else "%10s" % "-")
        unreal = ("%+9.2f%%" % r["unrealised_pct"]
                  if r["unrealised_pct"] is not None else "%10s" % "-")
        when = r["entry_bar_date"] or r["entry_target_date"] or ""
        out.append(
            f"  {r['ticker']:<8}{(r['category'] or '?'):<20}"
            f"{r['status']:<15}{when:<12}{entry_px}{unreal}"
            f"  {(r['headline'] or '')[:52]}")
    return "\n".join(out)


def recent_trades(conn, limit=15):
    out = [_bar(f"LAST {limit} CLOSED LEGS")]
    rows = conn.execute(
        "SELECT t.ticker, t.category, l.horizon, l.exit_bar_date, l.ret_pct, "
        "       l.pnl_aud, l.excess_pct "
        "FROM trade_legs l JOIN trades t ON t.id = l.trade_id "
        "ORDER BY l.closed_utc DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        out.append("  None.")
        return "\n".join(out)
    out.append(f"  {'ticker':<8}{'category':<20}{'h':>4}{'exit':>12}"
               f"{'return':>10}{'$':>10}{'excess':>10}")
    for r in rows:
        out.append(f"  {r['ticker']:<8}{(r['category'] or '?'):<20}"
                   f"{('t+' + str(r['horizon'])):>4}{r['exit_bar_date']:>12}"
                   f"{r['ret_pct']:>+9.2f}%{r['pnl_aud']:>+10.2f}"
                   f"{(('%+.2f%%' % r['excess_pct']) if r['excess_pct'] is not None else '-'):>10}")
    return "\n".join(out)


def health(conn):
    out = [_bar("HEALTH")]
    for key in ("started_utc", "last_poll_utc", "last_settle_utc",
                "poll_cycles", "consecutive_poll_failures"):
        row = conn.execute("SELECT value FROM runtime WHERE key=?",
                           (key,)).fetchone()
        out.append(f"  {key:<28}{row['value'] if row else '-'}")
    return "\n".join(out)


def build(limit=15):
    conn = store.init()
    parts = [
        "=" * 72,
        f"OpenClaw shadow book   |   generated {store.utcnow()}",
        f"universe: market cap <= ${config.MAX_MARKET_CAP / 1e6:.0f}m   "
        f"notional/trade: ${config.NOTIONAL_PER_TRADE:,.0f}   "
        f"horizons: {config.HORIZONS}",
        "=" * 72,
        health(conn),
        filter_summary(conn),
        book_summary(conn),
        horizon_summary(conn),
        category_summary(conn),
        open_positions(conn),
        recent_trades(conn, limit),
        "",
        "System-design exercise, not financial advice. Shadow P&L assumes "
        "fills that never had to",
        "compete for liquidity - treat the entry price as approximate and "
        "lean pessimistic.",
    ]
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description="OpenClaw shadow book report")
    ap.add_argument("--limit", type=int, default=15,
                    help="how many recently closed legs to list")
    args = ap.parse_args()
    print(build(args.limit))


if __name__ == "__main__":
    main()
