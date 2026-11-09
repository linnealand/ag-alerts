#!/usr/bin/env python3
"""American Girl sale alerts.

Fetches the store's collection feeds, finds in-stock products at 60% off or
more, and sends an ntfy push for each product that newly qualifies.

Usage:
    NTFY_TOPIC=your-long-random-topic python3 check_sales.py
    python3 check_sales.py --dry-run         # print alerts, don't send or save

Environment:
    NTFY_TOPIC   ntfy topic name (required unless --dry-run)
    NTFY_SERVER  ntfy server (default https://ntfy.sh)

Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

STORE = "https://www.americangirl.com"
COLLECTIONS = ["american-girl-last-chance", "sale"]
SALE_PAGE = f"{STORE}/collections/sale"
PAGE_SIZE = 250
MAX_PAGES = 20  # guard against an endless pagination loop

MAX_DISCOUNT_RATIO = Decimal(40)  # price must be <= 40% of compare_at (60%+ off)
REAPPEAR_DAYS = 7  # a product must be gone this long before it can alert again
MIN_COUNT_RATIO = Decimal("0.5")  # safety check against a bad/partial response
MAX_INDIVIDUAL_ALERTS = 5
HEARTBEAT_WEEKDAY = 0  # Monday

REQUEST_TIMEOUT = 30
REQUEST_PAUSE = 1.0  # seconds between feed requests
RATE_LIMIT_RETRIES = 1  # one retry after HTTP 429/503; the next hourly run is the real retry
MAX_RETRY_WAIT = 60  # cap on Retry-After, so a stalled run stays short

DEFAULT_STATE = Path(__file__).with_name("state.json")


class CheckError(Exception):
    """A failure that should stop the run without changing state."""


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def http_get_json(url: str, retries: int = RATE_LIMIT_RETRIES) -> dict:
    req = urllib.request.Request(url)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status != 200:
                    raise CheckError(f"GET {url} returned HTTP {resp.status}")
                return json.load(resp)
        except urllib.error.HTTPError as e:
            # Respect the store's rate limit: wait as asked, then try again.
            if e.code in (429, 503) and attempt < retries:
                wait = retry_after_seconds(e.headers.get("Retry-After"))
                print(f"HTTP {e.code}; waiting {wait}s before retrying", file=sys.stderr)
                time.sleep(wait)
                continue
            raise CheckError(f"GET {url} failed: {e}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise CheckError(f"GET {url} failed: {e}") from e
    raise CheckError(f"GET {url} failed after {retries} retries")


def retry_after_seconds(header: str | None) -> int:
    try:
        return min(max(int(header), 1), MAX_RETRY_WAIT)
    except (TypeError, ValueError):
        return 60


def fetch_collection(handle: str, get=http_get_json, pause=REQUEST_PAUSE) -> list[dict]:
    """Fetch every product in a collection, 250 per page (no sort_by)."""
    products: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{STORE}/collections/{handle}/products.json?limit={PAGE_SIZE}&page={page}"
        data = get(url)
        batch = data.get("products")
        if not isinstance(batch, list):
            raise CheckError(f"Unexpected response from {url}: no 'products' list")
        products.extend(batch)
        if len(batch) < PAGE_SIZE:
            return products
        if pause:
            time.sleep(pause)
    raise CheckError(f"Collection {handle} has more than {MAX_PAGES} pages; stopping")


def fetch_all(collections=COLLECTIONS, get=http_get_json, pause=REQUEST_PAUSE) -> list[dict]:
    """Fetch all collections and de-duplicate by product ID."""
    merged: dict[int, dict] = {}
    for i, handle in enumerate(collections):
        if i and pause:
            time.sleep(pause)
        for p in fetch_collection(handle, get=get, pause=pause):
            merged.setdefault(p["id"], p)
    return list(merged.values())


# --------------------------------------------------------------------------
# Matching rules
# --------------------------------------------------------------------------

def to_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def variant_qualifies(v: dict) -> bool:
    """Available, priced above 0, has a compare_at above 0, and is 60%+ off."""
    if v.get("available") is not True:
        return False
    price = to_decimal(v.get("price"))
    compare_at = to_decimal(v.get("compare_at_price"))
    if price is None or price <= 0:
        return False
    if compare_at is None or compare_at <= 0:
        return False
    return price * 100 <= compare_at * MAX_DISCOUNT_RATIO


def discount_percent(v: dict) -> int:
    """Whole-number percent off, rounded down (so 59.9% never shows as 60%)."""
    price = to_decimal(v["price"])
    compare_at = to_decimal(v["compare_at_price"])
    return int((1 - price / compare_at) * 100)


def tags_of(product: dict) -> list[str]:
    tags = product.get("tags") or []
    if isinstance(tags, str):  # some Shopify feeds return a comma string
        tags = tags.split(",")
    return [t.strip().lower() for t in tags]


def is_hidden(product: dict) -> bool:
    # Some products carry both "false" and "true"; any "true" means hidden.
    return "excludefromsearch: true" in tags_of(product)


def is_excluded(product: dict) -> bool:
    # Clothing and accessories for people (girls, little girls, adults),
    # including girl-and-doll matching sets. The doll half of a set is usually
    # also sold on its own under the doll clothing tag, so it still alerts.
    tags = tags_of(product)
    return ("agcat: girls clothing and accessories" in tags
            or "agcat: adult clothing and accessories" in tags)


def is_final_sale(product: dict) -> bool:
    return "sale: final sale" in tags_of(product)


def find_matches(products: list[dict]) -> dict[str, dict]:
    """Return {product_id: match info} for every qualifying product."""
    matches: dict[str, dict] = {}
    for p in products:
        if is_excluded(p):
            continue
        good = [v for v in p.get("variants", []) if variant_qualifies(v)]
        if not good:
            continue
        # Headline the biggest discount; ties go to the lowest price.
        best = max(good, key=lambda v: (discount_percent(v), -to_decimal(v["price"])))
        sizes = [v["title"] for v in good if v.get("title") and v["title"] != "Default Title"]
        images = p.get("images") or []
        matches[str(p["id"])] = {
            "title": p.get("title", "").strip(),
            "url": f"{STORE}/products/{p['handle']}",
            "image": images[0]["src"] if images else None,
            "price": to_decimal(best["price"]),
            "compare_at": to_decimal(best["compare_at_price"]),
            "percent": discount_percent(best),
            "sizes": sizes,
            "hidden": is_hidden(p),
            "final_sale": is_final_sale(p),
        }
    return matches


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict | None:
    """Return the saved state, or None on a first run."""
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("last_product_count", 0)
    state.setdefault("items", {})
    return state


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def safety_check(product_count: int, state: dict | None) -> None:
    if product_count == 0:
        raise CheckError("Feed returned no products")
    previous = (state or {}).get("last_product_count") or 0
    if previous and Decimal(product_count) < Decimal(previous) * MIN_COUNT_RATIO:
        raise CheckError(
            f"Product count dropped from {previous} to {product_count} "
            f"(below {MIN_COUNT_RATIO:.0%}); not trusting this response"
        )


def update_state(state: dict, matches: dict[str, dict], today: dt.date) -> list[str]:
    """Apply this run's matches to state. Returns IDs that should alert.

    Mutates state["items"] in place.
    """
    items = state["items"]
    new_ids: list[str] = []

    for pid, m in matches.items():
        tracked = items.get(pid)
        if tracked and tracked.get("missing_since"):
            gone_for = (today - dt.date.fromisoformat(tracked["missing_since"])).days
            if gone_for >= REAPPEAR_DAYS:
                tracked = None  # long enough gone: treat as new
        if tracked is None:
            items[pid] = {
                "title": m["title"],
                "first_alerted": today.isoformat(),
                "missing_since": None,
            }
            new_ids.append(pid)
        elif tracked.get("missing_since"):
            tracked["missing_since"] = None  # came back within the window

    for pid in list(items):
        if pid in matches:
            continue
        tracked = items[pid]
        if not tracked.get("missing_since"):
            tracked["missing_since"] = today.isoformat()
        elif (today - dt.date.fromisoformat(tracked["missing_since"])).days >= REAPPEAR_DAYS:
            del items[pid]

    return new_ids


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def money(d: Decimal) -> str:
    return f"${d:,.2f}"


def item_notification(m: dict) -> dict:
    parts = [f"{money(m['price'])} (was {money(m['compare_at'])})."]
    if m["sizes"]:
        label = "size" if len(m["sizes"]) == 1 else "sizes"
        parts.append(f"In stock: {label} {', '.join(m['sizes'])}.")
    else:
        parts.append("In stock.")
    if m["final_sale"]:
        parts.append("Final sale.")
    if m["hidden"]:
        parts.append("Not on the sale page.")
    msg = {
        "title": f"{m['percent']}% off: {m['title']}",
        "message": " ".join(parts),
        "click": m["url"],
        "tags": ["shopping"],
    }
    if m["image"]:
        msg["attach"] = m["image"]
    return msg


def summary_notification(matches: list[dict], heading: str) -> dict:
    ordered = sorted(matches, key=lambda m: (-m["percent"], m["price"]))
    lines = [f"{m['percent']}% off {money(m['price'])}: {m['title']}" for m in ordered[:15]]
    if len(ordered) > 15:
        lines.append(f"...and {len(ordered) - 15} more")
    return {
        "title": heading,
        "message": "\n".join(lines),
        "click": SALE_PAGE,
        "tags": ["shopping"],
    }


def build_notifications(new_matches: list[dict]) -> list[dict]:
    if not new_matches:
        return []
    if len(new_matches) <= MAX_INDIVIDUAL_ALERTS:
        return [item_notification(m) for m in new_matches]
    return [summary_notification(new_matches, f"{len(new_matches)} new items at 60%+ off")]


def send_ntfy(msg: dict, topic: str, server: str) -> None:
    body = json.dumps({"topic": topic, **msg}).encode("utf-8")
    req = urllib.request.Request(
        server.rstrip("/"),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                raise CheckError(f"ntfy returned HTTP {resp.status}")
    except urllib.error.URLError as e:
        raise CheckError(f"ntfy send failed: {e}") from e


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run(args: argparse.Namespace, today: dt.date | None = None,
        fetch=fetch_all, send=send_ntfy) -> int:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    state_path = Path(args.state)
    topic = os.environ.get("NTFY_TOPIC", "")
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    if not args.dry_run and not topic:
        raise CheckError("NTFY_TOPIC is not set (use --dry-run to test without it)")

    state = load_state(state_path)
    first_run = state is None

    products = fetch()
    safety_check(len(products), state)

    matches = find_matches(products)
    working = state or {"last_product_count": 0, "items": {}}
    new_ids = update_state(working, matches, today)
    working["last_product_count"] = len(products)

    if first_run:
        # Save everything as seen and send one overview instead of N alerts.
        notes = [summary_notification(list(matches.values()),
                                      f"Sale alerts set up: {len(matches)} items at 60%+ off now")]
    else:
        notes = build_notifications([matches[pid] for pid in new_ids])

    if today.weekday() == HEARTBEAT_WEEKDAY and working.get("last_heartbeat") != today.isoformat():
        notes.append({
            "title": "Sale alerts still running",
            "message": f"Tracking {len(working['items'])} items at 60%+ off "
                       f"across {len(products)} sale products.",
            "tags": ["white_check_mark"],
        })
        working["last_heartbeat"] = today.isoformat()

    print(f"{len(products)} products, {len(matches)} qualifying, {len(new_ids)} new"
          + (" (first run)" if first_run else ""))

    if args.dry_run:
        for n in notes:
            print("\n" + json.dumps(n, indent=2, ensure_ascii=False))
        print("\n(dry run: nothing sent, state not saved)")
        return 0

    for n in notes:
        send(n, topic, server)
    # Only save after every send succeeded, so a failed send retries next run.
    save_state(state_path, working)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="print notifications instead of sending; don't save state")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="path to state.json")
    parser.add_argument("--test-notification", action="store_true",
                        help="send one test alert to NTFY_TOPIC and exit")
    args = parser.parse_args(argv)

    try:
        if args.test_notification:
            topic = os.environ.get("NTFY_TOPIC") or ""
            if not topic:
                raise CheckError("NTFY_TOPIC is not set")
            send_ntfy({"title": "Test alert", "message": "AG sale alerts are connected.",
                       "click": SALE_PAGE}, topic,
                      os.environ.get("NTFY_SERVER", "https://ntfy.sh"))
            print("Test notification sent.")
            return 0
        return run(args)
    except CheckError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
