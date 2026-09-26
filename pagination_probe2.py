import requests
import time
import json
from collections import Counter

API_URL = "https://asx.api.markitdigital.com/asx-research/1.0/markets/announcements"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www2.asx.com.au",
    "Referer": "https://www2.asx.com.au/",
}

ITEMS_PER_PAGE = 50
MAX_PAGE = 20            # push further than last time
DELAY_BETWEEN_PAGES = 3  # seconds — disambiguate throttle vs hard wall

all_items = []
errors = []

print(f"Probing pages 0..{MAX_PAGE}, {DELAY_BETWEEN_PAGES}s sleep between pages\n")

for page in range(MAX_PAGE + 1):
    params = {"page": page, "itemsPerPage": ITEMS_PER_PAGE}
    t0 = time.time()
    try:
        r = requests.get(API_URL, headers=HEADERS, params=params, timeout=20)
        elapsed = (time.time() - t0) * 1000
        if r.status_code != 200:
            print(f"page={page:>2}  HTTP {r.status_code}  ({elapsed:.0f} ms)")
            errors.append((page, r.status_code))
            time.sleep(DELAY_BETWEEN_PAGES)
            continue
        payload = r.json()
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        print(f"page={page:>2}  ERROR: {e}  ({elapsed:.0f} ms)")
        errors.append((page, str(e)))
        time.sleep(DELAY_BETWEEN_PAGES)
        continue

    items = (payload.get("data") or {}).get("items") or []
    if not items:
        print(f"page={page:>2}  EMPTY  ({elapsed:.0f} ms)")
        break

    for it in items:
        it["_page"] = page
    all_items.extend(items)

    newest = items[0].get("date")
    oldest = items[-1].get("date")
    print(f"page={page:>2}  n={len(items):>2}  newest={newest}  oldest={oldest}  ({elapsed:.0f} ms)")

    if len(items) < ITEMS_PER_PAGE:
        print(f"  -> short page, end of feed")
        break

    time.sleep(DELAY_BETWEEN_PAGES)

print("\n=== Field-by-field uniqueness across all items ===")
print(f"Total items collected: {len(all_items)}")

if all_items:
    sample_keys = sorted(all_items[0].keys())
    print(f"Keys in item 0: {sample_keys}")

    print("\n--- First raw item (pretty) ---")
    print(json.dumps({k: v for k, v in all_items[0].items() if k != '_page'}, indent=2, default=str)[:2000])

    print("\n--- Uniqueness per field ---")
    for key in sample_keys:
        if key == "_page":
            continue
        vals = [str(it.get(key)) for it in all_items]
        uniq = len(set(vals))
        sample_val = vals[0][:60] if vals else ""
        print(f"  {key:30s}  unique={uniq:>4}/{len(vals):>4}  sample={sample_val!r}")

    # Specifically: does documentKey look like a viable dedupe key?
    doc_keys = [it.get("documentKey") for it in all_items]
    dk_uniq = len(set(doc_keys))
    print(f"\ndocumentKey: {dk_uniq} unique / {len(doc_keys)} total")
    if dk_uniq < len(doc_keys):
        dupes = [k for k, c in Counter(doc_keys).items() if c > 1]
        print(f"  duplicates: {len(dupes)} keys repeat — examples: {dupes[:3]}")

print(f"\nErrors during run: {errors}")
