import requests
import time

API_URL = "https://asx.api.markitdigital.com/asx-research/1.0/markets/announcements"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www2.asx.com.au",
    "Referer": "https://www2.asx.com.au/",
}

ITEMS_PER_PAGE = 50
MAX_PAGE = 10

seen_ids = set()
prev_oldest = None

print(f"Probing {API_URL}")
print(f"itemsPerPage={ITEMS_PER_PAGE}, pages 0..{MAX_PAGE}\n")

for page in range(MAX_PAGE + 1):
    params = {"page": page, "itemsPerPage": ITEMS_PER_PAGE}
    t0 = time.time()
    try:
        r = requests.get(API_URL, headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"page={page:>2}  ERROR: {e}")
        break

    payload = r.json()
    data = payload.get("data", {}) or {}
    items = data.get("items", []) or []
    total = data.get("total") or data.get("totalCount") or data.get("totalItems")
    elapsed = (time.time() - t0) * 1000

    if not items:
        print(f"page={page:>2}  EMPTY  ({elapsed:.0f} ms)  total_field={total}")
        break

    ids = [it.get("id") for it in items]
    dups_in_page = len(ids) - len(set(ids))
    overlap_with_prior = sum(1 for i in ids if i in seen_ids)
    seen_ids.update(ids)

    def pick_date(it):
        for k in ("releaseTime", "documentDate", "publishedDate", "dateTime", "date"):
            v = it.get(k)
            if v:
                return k, v
        return None, None

    newest_k, newest_v = pick_date(items[0])
    oldest_k, oldest_v = pick_date(items[-1])

    monotonic = ""
    if prev_oldest and newest_v:
        monotonic = "  older-than-prev" if newest_v <= prev_oldest else "  OVERLAPS-prev"
    prev_oldest = oldest_v

    print(
        f"page={page:>2}  n={len(items):>2}  dup_in_page={dups_in_page}  "
        f"overlap_prior={overlap_with_prior}  "
        f"newest[{newest_k}]={newest_v}  oldest[{oldest_k}]={oldest_v}  "
        f"total={total}  ({elapsed:.0f} ms){monotonic}"
    )

    if len(items) < ITEMS_PER_PAGE:
        print(f"  -> short page, likely end of feed")
        break

print(f"\nUnique ids seen across all pages: {len(seen_ids)}")
