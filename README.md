# ASX-Microcap-Trader

## OpenClaw

ASX announcement poller, rule-based pre-filter, and a forward-only paper
simulation. One process, meant to be left running for weeks.

*System-design exercise, not financial advice. No broker, no capital, no
orders. Nothing here can place a trade.*

---

## Start here

```
setup.bat        once, installs dependencies
selftest.bat     verifies the plumbing, writes to a scratch database
run_openclaw.bat starts it, leave the window open, Ctrl+C stops it cleanly
report.bat       prints the ledger, safe to run while it is running
```

Run `selftest.bat` first. If it reports a failure, do not leave the poller
running: a failing market-cap lookup would reject every announcement, and a
failing price feed would leave the book full of trades that never enter.

---

## What it does

```
  ASX Markit Digital feed
        |
  [1] ingest ......... dedup by documentKey, archive raw JSON,
        |              tag market state (PRE_OPEN / OPENING_AUCTION /
        |              CONTINUOUS / CLOSING_AUCTION / AFTER_HOURS)
  [2] pre-filter ..... price-sensitive AND category whitelist AND
        |              market cap <= $100m AND body length
        |
  [3] shadow book .... assumed entry, slippage haircut both sides,
                       parallel t+1 / t+5 / t+20 exits, marked daily
```

Stage 3 of the pipeline doc (the LLM reasoning layer) is deliberately not
here. This is the thin vertical slice: ingestion, a deterministic filter, and
an honest ledger. Adding the agent before there is a corpus to test it
against is the horizontal-layering trap.

---

## The pre-filter

Checks run cheapest-first, so the PDF download only happens for candidates
that already survived everything else.

| # | Check | Reject code |
|---|---|---|
| 1 | Ticker is a 3-letter ordinary-share code | `not_ordinary_shares` |
| 2 | Not on the manual exclusion list | `ticker_excluded` |
| 3 | ASX price-sensitive flag set | `not_price_sensitive` |
| 4 | Headline not administrative noise | `headline_excluded` |
| 5 | Headline matches a category | `no_category_match` |
| 6 | Category is one we trade | `category_not_tradeable` |
| 7 | Market cap resolves and is <= $100m | `mcap_unknown`, `mcap_above_ceiling` |
| 8 | Extracted body >= 400 characters | `thin_body` |

Categories: `CORPORATE_ACTION`, `DRILLING_RESULTS`, `RESOURCE_UPGRADE`,
`CLINICAL_REGULATORY`, `CONTRACT_WIN`, `PRODUCTION`, `EARNINGS`.
`CAPITAL_RAISE` is recognised but not traded (structurally negative for a
long-only book) - it is there so the event study can look at it later.

Two deliberate asymmetries:

- **A PDF whose text extraction fails is not rejected.** Scanned image PDFs
  are common in micro-caps, and failure to extract is not evidence of a thin
  announcement. Those pass with a `body_unavailable` flag, and the report
  counts them.
- **An unresolvable market cap is rejected.** For a size-constrained
  strategy, "cannot confirm it is small" means skip. The report warns
  loudly if `mcap_unknown` starts dominating, because that means the lookup
  is broken rather than the universe being wrong.

Every announcement is written to the database whether it passed or not, with
its reason code. That reject corpus is what Sprint 0.2's event study needs,
and a filter you cannot audit is a filter you cannot trust.

---

## The shadow book

Forward-only. Starts from now, never replays history.

**Assumed entry**, from the market state at publication:

| State | Assumed fill |
|---|---|
| `PRE_OPEN` | that day's open |
| `OPENING_AUCTION` | wait for 10:10, so that day's close |
| `CONTINUOUS` | next print after the decision, so that day's close |
| `CLOSING_AUCTION` | next session's open |
| `AFTER_HOURS` / non-trading day | next session's open |

**Slippage haircut**, applied on *both* sides:

| Market cap | Haircut |
|---|---|
| under $50m | 3.0% |
| $50m - $100m | 1.5% |

With no order book, the assumed entry price is the only number that can
quietly lie to you. A real order would move a thin micro-cap and the shadow
book will never see that, so the haircut is the conservative floor, not
decoration. Treat entry prices as approximate and lean pessimistic.

**Exits** at t+1, t+5 and t+20 trading days, tracked in parallel. These are
three hypotheticals about the same event, not a sequence of positions, so
the report gives P&L per horizon and never adds them together.

**Bar finality.** A daily bar for the current session is partial while the
market is open - its "close" is just the last print. Entries and exits wait
for the session to finish (open fixed after 10:20, close after 16:20 Sydney).
Without this the book would invent P&L out of intraday noise.

**Prices are split- and dividend-adjusted.** Over 20 days an unadjusted
consolidation would show up as a fake 90% loss.

**Excess return** is measured against the S&P/ASX Small Ordinaries (`^AXSO`),
falling back to ASX 300 then ASX 200 if the index has no data. Same benchmark
as the 0.2.6 gate.

A trade whose entry bar never appears within 10 days (suspension, delisting)
is recorded as `VOID`, not as a loss.

---

## Files

```
run_openclaw.py        the loop
selftest.py            plumbing verification
openclaw/config.py     every tunable lives here
openclaw/feed.py       Markit Digital client, retries, PDF extraction
openclaw/filters.py    Stage 2 pre-filter
openclaw/marketstate.py  ASX session classification
openclaw/prices.py     yfinance wrapper, market cap, daily bars
openclaw/shadowbook.py the paper simulation
openclaw/pipeline.py   poll cycle
openclaw/report.py     ledger report
data/openclaw.sqlite   everything persistent
data/raw/YYYY-MM-DD.jsonl  untouched feed JSON, append-only
data/logs/openclaw.log rotating, 5MB x 5
```

`seen_keys.txt` from the 0.1 poller is imported into the database on first
run, so nothing already processed gets re-emitted. It only happens once.

---

## Tuning

Everything is in `openclaw/config.py`. The ones you will actually touch:

- `MAX_MARKET_CAP` - universe ceiling
- `TRADEABLE_CATEGORIES` - which categories the book takes
- `CATEGORY_PATTERNS` / `HEADLINE_EXCLUSIONS` - the regexes
- `HORIZONS` - exit horizons
- `NOTIONAL_PER_TRADE` - only affects the dollar columns
- `POLL_INTERVAL_ACTIVE` - 60s during 07:00-19:00 Sydney weekdays,
  600s otherwise

After changing anything, run `selftest.bat` and restart.

---

## Running for weeks

- Poll failures never kill the loop. Backoff scales with consecutive
  failures up to 30 minutes, and the log shouts after 10 in a row.
- SQLite is in WAL mode with immediate commits. A power-off loses at most
  the item in flight.
- Dedup is from the database, so restarts do not re-emit anything.
- The log rotates, so it cannot fill the disk.
- `report.bat` shows `last_poll_utc`, `last_settle_utc` and
  `consecutive_poll_failures` under HEALTH. Check that before you trust
  anything else in the report.

The feed is a public endpoint with no contract behind it. If it starts
returning 401/403 the log says so explicitly - the browser-derived headers in
`config.HEADERS` are the first thing to refresh.

---

## What this is not

No historical replay. No order-book or L2 simulation. No fill matching,
no market-impact model. No IBKR. No LLM. Those are all real eventually and
none of them are needed for a forward shadow book that tracks hypothetical
P&L honestly.

## Reading the report

Evidence accumulates at the rate the market produces qualifying
announcements, which is slow. That is the point. The report flags any
category cell with fewer than 10 observations as anecdote.

The decision signal is whether live out-of-sample shadow P&L tracks what the
0.2 event study predicted. If it does, that is corroboration. If the live
edge is materially worse, that is the backtest-versus-reality gap surfacing,
and it wants investigating before any capital goes near it.
