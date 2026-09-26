"""
ASX announcement feed client (Markit Digital).

The 0.1 poller talked to MarketIndex, which now sits behind Cloudflare and
returns a hard 403. This module uses the Markit Digital endpoint that the
pagination probes confirmed working.

Every network call retries with jittered exponential backoff and returns
None on exhaustion. Nothing in here raises into the main loop.
"""
import io
import random
import time

import requests

from . import config
from .log import get

log = get("feed")

_session = None


def session():
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(config.HEADERS)
        _session = s
    return _session


def reset_session():
    """Drop the pooled connections. Called after a run of failures in case a
    stale keep-alive connection is the problem."""
    global _session
    if _session is not None:
        try:
            _session.close()
        except Exception:
            pass
    _session = None


def fetch_page(page, max_attempts=None):
    """Fetch one page of the announcements feed. Returns a list of items, or
    None if the page could not be retrieved."""
    max_attempts = max_attempts or config.MAX_FETCH_ATTEMPTS
    params = {"page": page, "itemsPerPage": config.ITEMS_PER_PAGE}

    for attempt in range(1, max_attempts + 1):
        try:
            r = session().get(config.API_URL, params=params,
                              timeout=config.HTTP_TIMEOUT)
            if r.status_code == 200:
                payload = r.json()
                items = (payload.get("data") or {}).get("items") or []
                return items
            if r.status_code in (401, 403):
                log.error("page=%s HTTP %s - the endpoint is rejecting us. "
                          "Headers may need refreshing from a live browser "
                          "session.", page, r.status_code)
            else:
                log.warning("page=%s HTTP %s (attempt %s/%s)",
                            page, r.status_code, attempt, max_attempts)
        except requests.exceptions.RequestException as e:
            log.warning("page=%s network error: %s (attempt %s/%s)",
                        page, e, attempt, max_attempts)
        except ValueError as e:
            log.warning("page=%s bad JSON: %s (attempt %s/%s)",
                        page, e, attempt, max_attempts)

        if attempt == max_attempts:
            break
        time.sleep(min(30, 2 ** attempt) + random.uniform(0, 1))

    reset_session()
    return None


def pdf_url(doc_key):
    return config.PDF_URL_TEMPLATE.format(key=doc_key)


def extract_pdf_text(doc_key):
    """Download and text-extract an announcement PDF.

    Returns (text, error). text is None when extraction failed outright,
    which the filter treats differently from a genuinely short document:
    a scanned image PDF is not evidence of a thin announcement.
    """
    import pypdf

    url = pdf_url(doc_key)
    try:
        r = session().get(url, timeout=config.HTTP_TIMEOUT, stream=True)
        r.raise_for_status()
        blob = b""
        for chunk in r.iter_content(65536):
            blob += chunk
            if len(blob) > config.PDF_MAX_BYTES:
                return None, "pdf_too_large"
        reader = pypdf.PdfReader(io.BytesIO(blob))
        parts = []
        for page in reader.pages:
            try:
                txt = page.extract_text()
            except Exception:
                txt = None
            if txt:
                parts.append(txt)
        text = "\n".join(parts).strip()
        if not text:
            return None, "no_extractable_text"
        return text, None
    except requests.exceptions.RequestException as e:
        return None, f"download_failed: {e}"
    except Exception as e:
        return None, f"parse_failed: {e}"


def normalise(item):
    """Map a raw feed item onto the fields we rely on downstream."""
    return {
        "doc_key": item.get("documentKey"),
        "ticker": (item.get("symbol") or "").strip().upper(),
        "headline": (item.get("headline") or "").strip(),
        "published_raw": item.get("date"),
        "price_sensitive": bool(item.get("isPriceSensitive")),
        "pages": item.get("numberOfPages"),
    }
