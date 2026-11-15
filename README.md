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

harvest.bat      Sprint 0.2.1: pull two years of announcement history
study.bat        Sprint 0.2.2-0.2.6: the event study and the gate readout
tests.bat        offline tests, no network
```

Run `selftest.bat` first. If it reports a failure, do not leave the poller
running: a failing market-cap lookup would reject every announcement, and a
failing price feed would leave the book full of trades that never enter.

`harvest.bat` and `study.bat` are separate from the poller. They use their own
database and log file under `data/research/`, so they can run while the poller
is running and cannot touch the live ledger.

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

## Research: history and the 0.2.6 gate

The forward book answers the question slowly, at the rate the market
produces announcements. The event study answers it now, from history, using
the same filter, the same entry rules, the same slippage tiers and the same
benchmark, so the two can be compared like for like.

```
harvest.bat     about 3 minutes for 24 months, resumable
study.bat       about 10 minutes the first time (price download), then seconds
```

**What the feed allows** (probed 15 Nov 2026). Plain paging stops at 10,000
items, about three and a half weeks. The feed also takes `dateStart` and
`dateEnd` (inclusive Sydney dates), `priceSensitiveOnly=true`, and up to 2,000
items per page, and the date filter reaches back at least to 2010. So history
is harvested in date windows, newest first, each small enough to sit under the
cap. The harvester refuses to store a window if the feed ignores a filter
(it silently ignores parameters it does not know), so a changed endpoint
cannot fill the corpus with the live feed filed under old dates. It paces
itself at one request every 2.5 seconds on purpose: the endpoint is
unofficial and there is nothing to gain by going faster.

**What the study does.**

- Replays every price-sensitive announcement through `filters.evaluate`, the
  same function the live poller uses. The PDF body-length check is skipped (it
  would cost a download per candidate).
- Reconstructs market cap as at the announcement: share count as reported at
  the time, times the price actually quoted that day (split ratios unwound),
  median of the five sessions before it. Today's share count would be wrong by
  a multiple for companies that issue shares constantly. Checked against the
  ASX's own company directory: median ratio 1.00, 98% within 25%.
- Prices each trade by the shadow book's rules and checks it against the live
  ledger code: `tests/test_eventstudy.py` runs the real `shadowbook.settle()`
  and the study over the same prices and requires identical entry day, entry
  price, exit days and returns.
- Drops legs that cross a split Yahoo recorded but never applied to the older
  prices (it happens: one name showed a 19,900% return from a 1-for-200
  consolidation).

**Reading the report** (`data/research/eventstudy_report.txt`, also printed).
Start at READOUT, then the gate table.

| Column | Meaning |
|---|---|
| `excess` | mean net return after the haircut on both sides, minus the benchmark |
| `gross`, `w.gross` | the same before any cost, and with the top and bottom 5% clipped |
| `t(g)` | clustered t-stat of gross excess: is there a price reaction at all |
| `BE%` | the per-side cost that would bring mean excess to zero. Compare it with the 3.0% and 1.5% the book assumes. `<0` means the move does not beat the benchmark even for free |
| `t(cl)` | t-stat of net excess, clustered by entry date |
| `stress` | net excess with a half-tick cost floor and no fill on zero-volume sessions |
| `H1`, `H2` | net excess in each half of the sample |
| `gate` | `PASS` is the plan's wording (N >= 30, net excess > 0). `PASS*` also needs a positive median, clustered t >= 2, both halves positive, and positive under stress |

The CONTROL section matters most when reading a negative result. For each real
trade it enters the same stock on other pre-open mornings, 30 to 90 days
either side (same season, holding windows clear of the announcement), under
identical rules. The announcement effect is real minus placebo, paired per
trade. Without it you cannot tell "the announcement does nothing" from "these
stocks lag the benchmark anyway".

**Limits, so they are not forgotten.** Daily bars cannot see the order book or
anything inside a session, so this cannot test whether a fast reader beats
retail to the price. Companies delisted since are missing from Yahoo, so
results lean flattering. The haircut tiers are an assumption, not a
measurement. Two years is one market regime. A cell tested among dozens will
sometimes clear a gate by luck, which is what `PASS*` and the placebo are for.

The harvested corpus is for private, personal research (ASX terms). It lives
in `data/research/`, which is gitignored. Do not commit it or pass it on.

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

openclaw/harvest.py    Sprint 0.2.1 historical harvest
openclaw/histstore.py  research database (corpus, price cache)
openclaw/histprices.py cached history, point-in-time market cap, split checks
openclaw/eventstudy.py Sprint 0.2.2-0.2.6 event study, control, gate
tests/                 offline tests (tests.bat)
data/research/         corpus, price cache, report, trades CSV (gitignored)
data/logs/research.log research tools log, separate from the poller's
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
- `TICKER_PATTERN` - three characters, letters or digits (MA1, 4DX, S32 are
  ordinary shares; a letters-only pattern discarded about 10% of announcements)

The research tools read the same filter and slippage settings, so a change
here shows up in the next `study.bat` run with no extra work. Their own knobs
(harvest pacing, window sizes, liquidity bands, placebo settings, the gate's
N) are in the Research section of `config.py`.

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
