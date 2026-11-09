# American Girl Sale Alerts: Implementation Plan

**Goal:** Get a phone notification when an in-stock American Girl item newly reaches 60% off or more.

**Approach:** A small Python script runs every hour on GitHub Actions. It reads the store's product feeds, compares the results with the items it has already seen, and sends a push notification through ntfy for anything new.

Data in this plan comes from the live feeds on 2026-11-04.

## What the feed looks like

Your URL works without a login or API key. It returns JSON with each product's title, handle, tags, images and variants (one variant per size or edition). Each variant has the fields we need: `price`, `compare_at_price` and `available`.

| Collection | Products in feed | In stock | In stock at 60%+ off | Hidden from the page |
|---|---:|---:|---:|---:|
| `american-girl-last-chance` | 69 | 44 | 14 | 10 |
| `sale` | 582 | 484 | 15 | 29 |

The 15 in `sale` include all 14 from Last Chance, so both collections together have 15 qualifying products today. The extra one is Snow Pretty Top for Girls ($12, was $30). One of the 15 is a hidden product, described in the next section.

`robots.txt` allows `/collections/` paths but disallows `sort_by` URLs, so the script shouldn't use sorting. It needs about 4 requests per run, which is light for a personal hourly check.

## Why the feed and the page show different counts

**The feed includes products that the page hides.** The Last Chance page shows 59 products, while the feed returns 69. The 10 extra products all have the tag `ExcludeFromSearch: true`, and removing them matches the page exactly:

| | Total | In stock | Sold out |
|---|---:|---:|---:|
| Feed | 69 | 44 | 25 |
| Hidden by `ExcludeFromSearch: true` | 10 | 1 | 9 |
| **Feed minus hidden (matches the page)** | **59** | **43** | **16** |

This means the feed isn't missing anything from Last Chance. The store's own collection data still reports 406 products there (and 831 for `sale`, against 582 in the feed). That number doesn't reflect what shoppers see, so the script shouldn't use it.

**One hidden product qualifies today:** Merry Everything PJs for Little Girls, $14 (was $44), 68% off, size 2T. It has both `ExcludeFromSearch: false` and `ExcludeFromSearch: true` tags, so the script must look for the `true` tag instead of reading just the first match. Its product page loads and the store reports 2T as available, but I haven't confirmed it can be checked out.

**Default:** Include hidden products and mark them "not on the sale page". They could be real bargains that other shoppers can't easily find, and a bad alert would be easy to ignore. The catch is that some may turn out not to be buyable.

## What counts as a match

A product qualifies when at least one of its variants meets all of these conditions:

1. `available` is true.
2. `price` is above 0.
3. `compare_at_price` is present and above 0.
4. `price` is at most 40% of `compare_at_price`.

The live data shows why each rule matters:

| Rule | Why it's needed |
|---|---|
| Check every variant, not just the first | Holiday Cheer PJ Pants: size 3 is $10 (50% off) and out of stock, while size 6 is $4.95 (75% off) and in stock. Reading only the first variant would miss it. |
| Use the `available` field | 35 Last Chance products have a variant at 60%+ off, but only 14 can be bought. |
| Don't use the `Inventory: In-Stock` tag | Only 24 Last Chance products have this tag, but 44 have something available. |
| Ignore a price of $0.00 | Three variants are listed at $0.00. All are out of stock and look like placeholders, not 100% discounts. |
| Skip a missing `compare_at_price` | Three Spring Leggings variants have no original price, so no discount can be calculated. |
| Include exactly 60% and calculate with `Decimal` | Kira's Wallaby Care Set is exactly 60% off ($12, was $30). Floating-point maths could push a case like this just under the line, so check `price * 100 <= compare_at * 40` using `Decimal`. |

**Sizes:** Most qualifying clothing has only some sizes left, so each alert should list the sizes in stock.

## What counts as new

The script alerts once per product, and only when that product qualifies now and isn't already being tracked.

| Situation | Alert? |
|---|---|
| Product appears at 60%+ off | Yes |
| Product was 50% off and drops to 60%+ | Yes |
| Out-of-stock product at 60%+ comes back in stock | Yes, if it hasn't qualified for 7 days or more |
| Tracked product stops qualifying, then returns within 7 days | No, this avoids repeat alerts when stock flickers |
| Tracked product gets cheaper, such as 60% to 75% off | No (could be added later) |
| Tracked product gets another size back | No (could be added later) |

## How each run works

1. Fetch each collection, 250 products per page, until a page has fewer than 250 products.
2. Merge the collections and remove duplicates by product ID.
3. Run a safety check. If any request fails, or the total drops below 50% of the previous run's count, exit with an error without changing anything.
4. Find the qualifying products and compare them with `state.json`.
5. Send notifications for new products.
6. Save `state.json`, but only after the notifications succeed, so a failed send is retried on the next run.

The safety check matters because a bad response could otherwise make every product look gone. After 7 days, the whole list would then alert again.

`state.json` tracks products by ID and only changes when a product starts or stops qualifying:

```json
{
  "last_product_count": 587,
  "items": {
    "7637199978686": {
      "title": "A Fin-tastic Surprise Book (WellieWishers)",
      "first_alerted": "2026-11-04",
      "missing_since": null
    }
  }
}
```

(The count and item above are examples.)

## Notifications

Use [ntfy](https://ntfy.sh): it's free, needs no account, and has iPhone and Android apps. The script POSTs to it, and the phone receives a push.

```text
Title:   75% off: Holiday Cheer PJ Pants for Girls
Message: $4.95 (was $20.00). In stock: size 6. Final sale.
Tap:     opens the product page
Image:   product photo
```

- **Several new items:** Send one notification per product, up to 5 per run. If there are more than 5, send one summary that links to the sale page.
- **Encoding:** Use ntfy's JSON format, because titles contain characters like `™` that can break HTTP headers.
- **Privacy:** Anyone who knows an ntfy topic name can read it, so use a long random name and store it as a GitHub secret. The alerts contain nothing personal.

## Hosting

Run the script on GitHub Actions, so it keeps working when your Mac is asleep. Sale items can sell out quickly, so this matters more than it would for most scripts.

- **Repository:** Use a private repo. Hourly runs should fit within the free monthly Actions minutes, but check the current limits.
- **Schedule:** Run hourly. GitHub can start scheduled jobs late, which is fine for this use.
- **Saving state:** The workflow commits `state.json` back to the repo when it changes.
- **Dependencies:** None, because Python's standard library covers everything.

**Fallback:** Cloudflare sits in front of the store and could block GitHub's servers, even though requests from your Mac work. If that happens, run the same script on your Mac with `launchd`. It would pause while the Mac sleeps and run again after it wakes.

### Files

```text
ag-sale-alerts/
├── check_sales.py              # fetch, filter, compare, notify
├── test_check_sales.py         # tests for the matching rules
├── state.json                  # items already seen
├── fixtures/                   # saved sample responses for tests
└── .github/workflows/check.yml # hourly schedule
```

## Build steps

1. **Write the core logic:** Build fetching, matching and state tracking, with a `--dry-run` flag that prints instead of notifying. Test it against saved real responses, covering every rule in the tables above.
2. **Add notifications:** Install the ntfy app, create the topic and send a test alert.
3. **Do a first run:** Save today's 15 or so items as already seen, and send one summary of what's currently on sale instead of 15 separate alerts.
4. **Schedule it:** Add the GitHub Actions workflow, run it manually once, and confirm the store doesn't block it.
5. **Watch it for a week:** Compare alerts with the sale page and look for missed or duplicate items.

## How we'll know if it breaks

A broken checker looks exactly like a quiet week for sales, so it needs two signals:

- **Failed runs:** The script exits with an error, and GitHub emails you about the failed run.
- **Weekly check-in:** A short "still running, tracking N items" notification every Monday. If it stops arriving, the schedule has stopped.

## Known limitations

- **Promo codes:** Discounts are based on listed prices. Many products are tagged as promo-eligible, so an item listed at 50% off could reach 60% with a code, and the script wouldn't count it.
- **Reference prices:** The discount is calculated from the store's `compare_at_price`, which is the same "was" price shown to shoppers.
- **Timing:** There can be up to about an hour between a markdown and its alert, or longer if GitHub starts the job late.
- **Feed changes:** The store could change or remove this feed without notice. The failed-run email would flag that.

## Decisions to confirm

The plan uses these defaults. Tell me if you'd like to change any of them.

| Decision | Default | Alternative |
|---|---|---|
| Which collections to watch | Both `sale` and Last Chance | Last Chance only, which misses one item today |
| How to receive alerts | Phone push through ntfy | Email or Mac notifications |
| Where it runs | GitHub Actions | Your Mac, with no GitHub account but no checks while it sleeps |
| How often it checks | Hourly | Every 30 minutes for faster alerts on sought-after items |
| Whether to include products hidden from the page | Yes, marked "not on the sale page" | Skip them so alerts match the page exactly |
| Whether to alert on bigger discounts | No, new items only | Also alert when a tracked item's price drops further |
