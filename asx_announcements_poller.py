import io
import os
import random
import time

import pypdf
import requests

API_URL = "https://asx.api.markitdigital.com/asx-research/1.0/markets/announcements"
PDF_URL_TEMPLATE = "https://asx.api.markitdigital.com/asx-research/1.0/file/{key}"

POLL_INTERVAL_SECONDS = 60
ITEMS_PER_PAGE = 50

SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_keys.txt")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www2.asx.com.au",
    "Referer": "https://www2.asx.com.au/",
}


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def mark_seen(key):
    # Append-only persistence: crash-safe, never rewrites the file.
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(key + "\n")


def fetch_page(page, max_attempts=4):
    params = {"page": page, "itemsPerPage": ITEMS_PER_PAGE}
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.get(API_URL, headers=HEADERS, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            # 502/503/504/404 have all been observed as transient on this endpoint.
            print(f"  [retry] page={page} HTTP {r.status_code} (attempt {attempt}/{max_attempts})")
        except requests.exceptions.RequestException as e:
            print(f"  [retry] page={page} network error: {e} (attempt {attempt}/{max_attempts})")
        backoff = min(30, 2 ** attempt) + random.uniform(0, 1)
        time.sleep(backoff)
    return None


def extract_text_from_pdf(pdf_url):
    try:
        response = requests.get(pdf_url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        reader = pypdf.PdfReader(io.BytesIO(response.content))
        return "".join(page.extract_text() + "\n" for page in reader.pages).strip()
    except Exception as e:
        print(f"  [!] Failed to read PDF: {e}")
        return None


def process_item(item, seen):
    doc_key = item.get("documentKey")
    if not doc_key or doc_key in seen:
        return False

    ticker = item.get("symbol", "UNK")
    headline = item.get("headline", "No Title")
    date = item.get("date", "")
    price_sensitive = item.get("isPriceSensitive", False)
    flag = " [PRICE-SENSITIVE]" if price_sensitive else ""

    print(f"\n[NEW] {date}  {ticker}{flag}  {headline}")

    pdf_link = PDF_URL_TEMPLATE.format(key=doc_key)
    print(f"  -> {pdf_link}")
    text = extract_text_from_pdf(pdf_link)
    if text:
        print(f"  -> Extracted {len(text)} chars.")

    seen.add(doc_key)
    mark_seen(doc_key)
    return True


def poll_asx_direct():
    seen = load_seen()
    print(f"Loaded {len(seen)} previously-seen documentKeys from {SEEN_FILE}")
    print("Listening to ASX Markit Digital feed...")

    while True:
        payload = fetch_page(0)
        if payload is None:
            print(f"[{time.strftime('%X')}] Page 0 unreachable after retries, sleeping.")
        else:
            items = (payload.get("data") or {}).get("items") or []
            new_count = sum(process_item(it, seen) for it in items)
            print(f"[{time.strftime('%X')}] Polled. {len(items)} items in feed, {new_count} new.")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    poll_asx_direct()
