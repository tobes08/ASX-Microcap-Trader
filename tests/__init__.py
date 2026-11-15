"""Offline tests.

Fabricated data only. Nothing in here touches the network, the live ledger
or the research corpus: every path is pointed at a throwaway directory
before any openclaw module that opens a file gets imported.

    python -m unittest
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from openclaw import config  # noqa: E402

SCRATCH = tempfile.mkdtemp(prefix="openclaw_tests_")
config.DATA_DIR = SCRATCH
config.RAW_DIR = os.path.join(SCRATCH, "raw")
config.LOG_DIR = os.path.join(SCRATCH, "logs")
config.DB_PATH = os.path.join(SCRATCH, "ledger.sqlite")
config.RESEARCH_DIR = os.path.join(SCRATCH, "research")
config.HISTORY_DB_PATH = os.path.join(config.RESEARCH_DIR, "history.sqlite")
config.LEGACY_SEEN_FILE = os.path.join(SCRATCH, "nonexistent.txt")
config.HARVEST_MIN_INTERVAL = 0

from openclaw import log as oclog  # noqa: E402

oclog.setup(verbose=False, filename="tests.log")
