"""
Logging setup.

Rotating file handler, because this is meant to run for weeks. UTF-8 is
forced on both the file and the console: the 0.1 poller's log is full of
mojibake from Windows defaulting to cp1252.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from . import config

_configured = False


def setup(verbose=True):
    global _configured
    if _configured:
        return logging.getLogger("openclaw")

    os.makedirs(config.LOG_DIR, exist_ok=True)
    logger = logging.getLogger("openclaw")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    fh = RotatingFileHandler(
        os.path.join(config.LOG_DIR, "openclaw.log"),
        maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if verbose:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    # yfinance is chatty and its warnings are rarely actionable.
    logging.getLogger("yfinance").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("peewee").setLevel(logging.ERROR)

    _configured = True
    return logger


def get(name):
    setup()
    return logging.getLogger("openclaw." + name)
