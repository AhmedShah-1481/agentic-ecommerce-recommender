"""Source product photographs from Unsplash and write them into the seed file.

Run by hand, never by the app, and never by the test suite. The point of that
separation is that a fresh clone gets photographs with no API key and no network:
this script writes the URLs it finds into `seed/products.json`, which is
committed, and `scripts/seed.py` loads them like any other product field.

    # once, with a free key from https://unsplash.com/developers in .env
    venv/Scripts/python scripts/fetch_images.py --dry-run   # show the queries
    venv/Scripts/python scripts/fetch_images.py --missing    # fill only the gaps
    venv/Scripts/python scripts/fetch_images.py             # fetch + rewrite seed
    venv/Scripts/python scripts/fetch_images.py --apply     # also patch a live DB

Why hotlinked CDN URLs rather than downloaded files: Unsplash's API terms require
it - the photo has to be served from their CDN so their view counting works. That
makes hotlinking the compliant choice for now rather than the lazy one. It is
still a dependency on someone else's uptime for our product imagery, which is why
`ProductVisual` degrades to a generated panel on `onError`, and why moving to
local files is a tracked follow-up in NOTES.md.

Requests are per (category, subcategory), not per product: matching at
subcategory level is what makes "Trail Running" and "Sandals" look different from
each other without hand-picking a photo for every SKU.

Budget, because the demo tier allows 50 requests/hour and a full pass costs more
than that: one search per pair (44) *plus* one download registration per product
(56) is ~100 requests, so a cold run will hit the limit around halfway. It stops
cleanly and writes what it has. Use `--missing` to finish - it skips pairs that
are already photographed, so the second pass costs only what is still needed.
Checking that a URL resolves is free: that hits the CDN, not the API.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.config import settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import Product  # noqa: E402
from app.services.catalog import safe_url  # noqa: E402

SEED_FILE = BASE_DIR / "seed" / "products.json"

API_ROOT = "https://api.unsplash.com"
USER_AGENT = "bazario-mart-seed/1.0"

# Baked into the stored URL so no rendering surface needs to know about resizing.
# 4:5 to match the `aspect-4/5` card and the detail hero; `crop=entropy` keeps the
# subject rather than the geometric centre, which matters on product shots where
# the object sits low in the frame.
IMAGE_PARAMS = {
    "w": "900",
    "h": "1125",
    "fit": "crop",
    "crop": "entropy",
    "q": "80",
    "fm": "jpg",
}

# A word of context per category, appended to the subcategory. "Running" alone
# returns marathon crowds; "Running shoes" returns shoes.
CATEGORY_HINTS = {
    "Apparel": "clothing",
    "Footwear": "shoes",
    "Home & Kitchen": "home kitchen",
    "Electronics & Audio": "audio tech",
    "Beauty": "skincare",
    "Outdoor": "outdoor camping",
}

# Subcategories where "<subcategory> <hint>" is ambiguous, or wrong, or merely
# weak. Keyed "Category|Subcategory" because two categories share several
# subcategory names - Accessories and Lighting exist under two each, and they want
# opposite photos.
#
# The default is left in place for the seven cases where it already reads as a
# product search ("Running shoes", "Tents outdoor camping"), and remains the
# fallback for any subcategory an admin invents later.
QUERY_OVERRIDES = {
    # Apparel
    "Apparel|Accessories": "leather belt wallet accessory",
    "Apparel|Denim": "denim jeans",
    "Apparel|Knitwear": "knitted wool sweater",
    "Apparel|Midlayers": "fleece midlayer jacket",
    "Apparel|Outerwear": "winter coat jacket",
    "Apparel|Shirts": "linen button shirt",
    "Apparel|Trousers": "trousers pants clothing",
    # Footwear
    "Footwear|Boots": "leather boots",
    "Footwear|Slippers": "wool slippers",
    # Home & Kitchen - "<sub> home kitchen" is actively misleading for most of
    # these: it returns kitchens rather than the object.
    "Home & Kitchen|Bath": "bath towels bathroom",
    "Home & Kitchen|Bedding": "bed linen bedding",
    "Home & Kitchen|Coffee & Tea": "coffee pour over brewing",
    "Home & Kitchen|Cookware": "cast iron pan cookware",
    "Home & Kitchen|Cutlery": "cutlery flatware",
    "Home & Kitchen|Kitchen Prep": "chopping board kitchen prep",
    "Home & Kitchen|Lighting": "table lamp warm interior",
    "Home & Kitchen|Rugs": "wool rug floor",
    "Home & Kitchen|Textiles": "folded linen textile",
    # Electronics & Audio
    "Electronics & Audio|Accessories": "usb cable tech accessory",
    "Electronics & Audio|Audio Components": "hifi amplifier turntable",
    "Electronics & Audio|Charging": "phone charger power bank",
    "Electronics & Audio|Earbuds": "wireless earbuds",
    "Electronics & Audio|Headphones": "headphones",
    "Electronics & Audio|Lighting": "desk lamp led",
    "Electronics & Audio|Speakers": "bluetooth speaker",
    # Beauty - the catalog uses British spelling, Unsplash is indexed in US
    # English, so "Moisturisers" has to be spelled the other way to find anything.
    "Beauty|Body Care": "body lotion bottle",
    "Beauty|Cleansers": "facial cleanser bottle",
    "Beauty|Hair Care": "shampoo bottle hair",
    "Beauty|Moisturisers": "moisturizer cream jar",
    "Beauty|Serums": "serum dropper bottle",
    "Beauty|Sun Care": "sunscreen bottle",
    "Beauty|Treatments": "skincare serum bottle",
    # Outdoor
    "Outdoor|Backpacks": "hiking backpack",
    "Outdoor|Camp Furniture": "camping chair campsite",
    "Outdoor|Camp Sleep": "sleeping bag camping",
    "Outdoor|Hiking Gear": "hiking trekking gear",
    "Outdoor|Hydration": "water bottle flask",
}


# --------------------------------------------------------------------------
# Seed file I/O
# --------------------------------------------------------------------------
def dump_products(rows: list[dict]) -> str:
    """Serialise the seed file in the style it is already written in.

    `json.dumps(indent=2)` would explode every `tags` array onto six lines and
    turn a content change into a 1,100-line diff. Lists render inline here, which
    round-trips the existing file byte for byte - there is a test asserting
    exactly that, because the guarantee is the only reason this exists.
    """
    out = ["["]
    for row_index, row in enumerate(rows):
        out.append("  {")
        keys = list(row.keys())
        for key_index, key in enumerate(keys):
            value = row[key]
            if isinstance(value, list):
                rendered = "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in value) + "]"
            else:
                rendered = json.dumps(value, ensure_ascii=False)
            tail = "," if key_index < len(keys) - 1 else ""
            out.append(f"    {json.dumps(key)}: {rendered}{tail}")
        out.append("  }" + ("," if row_index < len(rows) - 1 else ""))
    out.append("]")
    return "\n".join(out) + "\n"


def load_products() -> list[dict]:
    if not SEED_FILE.exists():
        raise SystemExit(f"Seed file missing: {SEED_FILE}")
    return json.loads(SEED_FILE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Unsplash
# --------------------------------------------------------------------------
def query_for(category: str, subcategory: str) -> str:
    override = QUERY_OVERRIDES.get(f"{category}|{subcategory}")
    if override:
        return override
    hint = CATEGORY_HINTS.get(category, "")
    return f"{subcategory} {hint}".strip()


def _get(url: str, *, key: str) -> tuple[dict | list, dict]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Client-ID {key}",
            "Accept-Version": "v1",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8")), dict(response.headers)


def search(query: str, *, key: str, per_page: int) -> tuple[list[dict], str | None]:
    """Candidate photos for one query, best match first, plus the rate-limit count.

    `content_filter=high` and `orientation=portrait` are both load-bearing: the
    filter because this is a shop, and the orientation because a landscape photo
    entropy-cropped to 4:5 loses most of its subject.
    """
    params = urllib.parse.urlencode(
        {
            "query": query,
            "per_page": per_page,
            "orientation": "portrait",
            "content_filter": "high",
        }
    )
    payload, headers = _get(f"{API_ROOT}/search/photos?{params}", key=key)
    remaining = headers.get("X-Ratelimit-Remaining")

    results = []
    for photo in payload.get("results", []) if isinstance(payload, dict) else []:
        raw = (photo.get("urls") or {}).get("raw") or ""
        if not raw:
            continue
        user = photo.get("user") or {}
        separator = "&" if "?" in raw else "?"
        results.append(
            {
                "photo_id": photo.get("id") or "",
                "url": raw + separator + urllib.parse.urlencode(IMAGE_PARAMS),
                "credit": user.get("name") or "Unknown",
                "credit_url": ((user.get("links") or {}).get("html")) or "",
                "download_location": ((photo.get("links") or {}).get("download_location")) or "",
            }
        )
    return results, remaining


def register_download(location: str, *, key: str) -> None:
    """Unsplash requires this be called when a photo is actually used.

    It is how photographers see their work being used, and complying is a
    condition of the API terms rather than a nicety. A failure here is logged and
    ignored: it must not cost us an image we have already chosen.
    """
    if not location:
        return
    try:
        _get(location, key=key)
    except Exception as exc:  # noqa: BLE001 - genuinely best-effort
        print(f"      ! download registration failed: {exc}")


def resolves(url: str) -> bool:
    """True when the URL actually serves an image right now.

    Every URL is checked before it is committed. The alternative is writing 56
    plausible-looking URLs into the seed file and finding out which of them were
    wrong from a grid of broken frames.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            content_type = response.headers.get("Content-Type", "")
            return response.status == 200 and content_type.startswith("image/")
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="print the query per subcategory and exit; no network, no key needed")
    parser.add_argument("--apply", action="store_true",
                        help="also patch image columns on an existing database")
    parser.add_argument("--per-page", type=int, default=6,
                        help="candidates fetched per subcategory (default 6)")
    parser.add_argument("--sleep", type=float, default=1.0,
                        help="seconds between API calls (default 1.0)")
    parser.add_argument("--only", default="",
                        help="restrict to subcategories containing this substring")
    parser.add_argument("--missing", action="store_true",
                        help="skip pairs that already have a photo on every product "
                             "(how you finish a run that hit the rate limit)")
    args = parser.parse_args()

    rows = load_products()

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row.get("category", ""), row.get("subcategory", ""))].append(row)

    pairs = sorted(groups)
    if args.only:
        needle = args.only.lower()
        pairs = [p for p in pairs if needle in f"{p[0]} {p[1]}".lower()]

    # Filtering by what is already done is the difference between finishing the
    # catalog and re-spending the whole hourly budget on pairs that are fine.
    if args.missing:
        already = len(pairs)
        pairs = [p for p in pairs if any(not r.get("image_url") for r in groups[p])]
        print(f"--missing: {already - len(pairs)} pairs already photographed, {len(pairs)} to go.")

    if args.dry_run:
        print(f"{len(pairs)} queries for {sum(len(groups[p]) for p in pairs)} products:\n")
        for category, subcategory in pairs:
            count = len(groups[(category, subcategory)])
            print(f"  {category:22} {subcategory:18} x{count}  ->  {query_for(category, subcategory)!r}")
        estimate = len(pairs) + sum(len(groups[p]) for p in pairs)
        print(f"\n~{estimate} API requests (one search per pair + one download registration"
              f" per product); demo tier allows 50/hour.")
        print("Nothing fetched and nothing written. Drop --dry-run to fetch.")
        return

    if not settings.unsplash_configured:
        raise SystemExit(
            "UNSPLASH_ACCESS_KEY is not set.\n"
            "  Free key: https://unsplash.com/developers (create an app, copy the Access Key)\n"
            "  Then add it to .env:  UNSPLASH_ACCESS_KEY=...\n"
            "  Or run with --dry-run to see the queries without fetching."
        )

    key = settings.unsplash_access_key.strip()
    matched = 0
    skipped: list[str] = []

    print(f"Fetching photos for {len(pairs)} subcategories...\n")

    for category, subcategory in pairs:
        products = groups[(category, subcategory)]
        query = query_for(category, subcategory)
        print(f"  {category} / {subcategory}  ({len(products)}x)  {query!r}")

        try:
            candidates, remaining = search(query, key=key, per_page=args.per_page)
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                print("\n  Rate limit reached (demo tier is 50/hour).")
                print("  Progress so far is written below; re-run later to finish the rest.")
                break
            print(f"      ! search failed: {exc.code} {exc.reason}")
            skipped.append(f"{category}/{subcategory}")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"      ! search failed: {exc}")
            skipped.append(f"{category}/{subcategory}")
            continue

        if not candidates:
            print("      ! no results")
            skipped.append(f"{category}/{subcategory}")
            continue

        # Distinct photo per product where the search gave us enough, so two
        # products in one subcategory never show the same frame.
        usable = [c for c in candidates if resolves(c["url"])]
        if not usable:
            print("      ! no candidate URL resolved")
            skipped.append(f"{category}/{subcategory}")
            continue

        for index, product in enumerate(products):
            choice = usable[index % len(usable)]
            product["image_url"] = choice["url"]
            product["image_credit"] = choice["credit"]
            product["image_credit_url"] = choice["credit_url"]
            register_download(choice["download_location"], key=key)
            matched += 1
            print(f"      {product['title'][:48]:50} <- {choice['credit']}")

        if remaining is not None:
            print(f"      ({remaining} API requests left this hour)")
        time.sleep(args.sleep)

    SEED_FILE.write_text(dump_products(rows), encoding="utf-8")
    covered = sum(1 for r in rows if r.get("image_url"))
    print(f"\nWrote {SEED_FILE.relative_to(BASE_DIR)} - {matched} products given a photo this run.")
    print(f"Catalog coverage: {covered}/{len(rows)} products have a photograph.")
    if covered < len(rows):
        print("Finish the rest with:  scripts/fetch_images.py --missing")
    if skipped:
        print(f"No photo for {len(skipped)}: {', '.join(skipped)}")
        print("Those keep their generated panel, which is a valid end state.")

    if args.apply:
        patch_database(rows)

    print("\nFor a fresh database:  venv/Scripts/python scripts/seed.py --reset")


def patch_database(rows: list[dict]) -> None:
    """Copy image fields from the seed rows onto matching products in the DB.

    Matched on title, the same key `seed_products` uses for its idempotency
    check. Writes the three image columns and nothing else - deliberately not
    routed through `catalog.update_product`, because these columns are outside
    `embedding_text()` and there is no vector work to do.
    """
    by_title = {row["title"]: row for row in rows if row.get("image_url")}
    if not by_title:
        print("Nothing to apply: no rows have an image_url.")
        return

    with session_scope() as db:
        updated = 0
        for product in db.query(Product).all():
            row = by_title.get(product.title)
            if not row:
                continue
            product.image_url = safe_url(row["image_url"])
            product.image_credit = row.get("image_credit", "")[:160]
            product.image_credit_url = safe_url(row.get("image_credit_url", ""))
            updated += 1
    print(f"Patched {updated} rows in the existing database.")


if __name__ == "__main__":
    main()
