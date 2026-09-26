"""
OpenClaw - ASX announcement poller + pre-filter + forward paper simulation.

Single process, meant to be left running for weeks. Ctrl+C stops it cleanly.

    python run_openclaw.py              # run forever
    python run_openclaw.py --once       # one poll + one settlement, then exit
    python run_openclaw.py --settle     # settlement only, no polling
    python run_openclaw.py --selftest   # check the plumbing, touch nothing

System-design exercise, not financial advice.
"""
import argparse
import os
import signal
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openclaw import config, log as oclog, pipeline, shadowbook, store  # noqa: E402
from openclaw.marketstate import poll_interval  # noqa: E402

log = oclog.setup()

_stop = False


def _handle_signal(signum, frame):
    global _stop
    _stop = True
    log.info("shutdown signal received, finishing this cycle")


def bootstrap():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.RAW_DIR, exist_ok=True)
    store.init()
    imported = store.import_legacy_seen_file(config.LEGACY_SEEN_FILE)
    if imported:
        log.info("imported %s document keys from the 0.1 seen_keys.txt - "
                 "those announcements will not be re-processed", imported)
    known = len(store.load_seen_keys())
    store.set_runtime("started_utc", store.utcnow())
    log.info("OpenClaw up. db=%s  known_keys=%s  universe<=$%.0fm  "
             "horizons=%s", config.DB_PATH, known,
             config.MAX_MARKET_CAP / 1e6, config.HORIZONS)


def run_forever():
    bootstrap()
    cycles = 0
    failures = 0
    last_settle = 0.0

    while not _stop:
        cycle_start = time.time()
        try:
            counts = pipeline.poll_once()
            cycles += 1
            store.set_runtime("poll_cycles", cycles)
            store.set_runtime("last_poll_utc", store.utcnow())

            if counts["pages_failed"]:
                failures += 1
            else:
                failures = 0
            store.set_runtime("consecutive_poll_failures", failures)

            log.info("poll: %s items, %s new passed, %s rejected, "
                     "%s already seen%s",
                     counts["items"], counts["passed"], counts["rejected"],
                     counts["seen"],
                     "  [FEED UNREACHABLE]" if counts["pages_failed"] else "")

            if failures and failures % 10 == 0:
                log.error("the feed has failed %s cycles in a row. If this "
                          "persists, the endpoint or its headers have "
                          "changed.", failures)
        except Exception:
            log.exception("poll cycle failed, continuing")
            failures += 1

        if time.time() - last_settle >= config.SETTLEMENT_INTERVAL:
            try:
                summary = shadowbook.settle()
                last_settle = time.time()
                if any(summary.values()):
                    log.info("settle: %s", summary)
            except Exception:
                log.exception("settlement pass failed, continuing")

        interval = poll_interval(datetime.now(timezone.utc),
                                 config.POLL_INTERVAL_ACTIVE,
                                 config.POLL_INTERVAL_QUIET)
        # Back off hard if the feed is down, so we are not hammering it.
        if failures:
            interval = min(1800, interval * min(failures, 8))

        elapsed = time.time() - cycle_start
        for _ in range(int(max(0, interval - elapsed))):
            if _stop:
                break
            time.sleep(1)

    log.info("OpenClaw stopped cleanly")


def run_once():
    bootstrap()
    counts = pipeline.poll_once()
    log.info("poll: %s", counts)
    summary = shadowbook.settle()
    log.info("settle: %s", summary)


def run_settle():
    bootstrap()
    log.info("settle: %s", shadowbook.settle())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true",
                   help="one poll plus one settlement, then exit")
    g.add_argument("--settle", action="store_true",
                   help="settlement pass only, no polling")
    g.add_argument("--selftest", action="store_true",
                   help="verify feed, prices and database, write nothing")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, ValueError):
        pass

    if args.selftest:
        from selftest import run as selftest_run
        sys.exit(0 if selftest_run() else 1)
    if args.once:
        run_once()
    elif args.settle:
        run_settle()
    else:
        run_forever()


if __name__ == "__main__":
    main()
