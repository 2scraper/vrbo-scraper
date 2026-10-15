#!/usr/bin/env python3
"""Assertions on a live canary run's output, shared by both canary jobs.

    python .github/canary_check.py --prefix canary_run --pages 1

Exits non-zero, printing one `::error::` line per failure, when the rows do
not show the PARSER working. Every floor here is about the parser, not about
how much inventory the site had that day: a dated search can genuinely hold
fifteen properties (measured 2026-10-15: "15 properties", "Only 18% of
properties are available on our site for your dates"), so the row floor is
low and the per-column checks carry the weight.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# The fewest rows a healthy served page has produced on a dated Orlando
# search: 13 (2026-10-10 and 2026-10-13) and 16 (2026-10-15), against 50 on
# most days. Below this the page was not read, whatever the exit code says.
MIN_ROWS_PER_PAGE = 10

# Columns every card carries. 100% on every measured run, so any shortfall
# means an anchor moved.
ALWAYS = ("title", "sku", "url", "page", "position", "listing_kind", "source")

# Present on almost every card. Price is lower than the rest because the site
# renders some cards with no price at all ("This property does not allow
# 2-night stays for those dates": 1 of 16 on 2026-10-15).
MOSTLY = (("price", 0.80), ("property_type", 0.90),
          ("expedia_property_id", 0.90))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--pages", type=int, required=True)
    args = ap.parse_args()

    rows_path = Path(f"{args.prefix}.json")
    meta_path = Path(f"{args.prefix}.meta.json")
    if not rows_path.is_file() or not meta_path.is_file():
        print(f"::error::{rows_path} or {meta_path} is missing: the run wrote "
              f"no output, so nothing was tested")
        return 1
    rows = json.loads(rows_path.read_text(encoding="utf-8"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    fail = []

    floor = MIN_ROWS_PER_PAGE * args.pages
    if len(rows) < floor:
        fail.append(f"only {len(rows)} rows across {args.pages} page(s), "
                    f"floor {floor}")

    for field in ALWAYS:
        missing = [r.get("sku") for r in rows if r.get(field) in (None, "")]
        if missing:
            fail.append(f"{field} missing on {len(missing)}/{len(rows)} rows")
    for field, share in MOSTLY:
        got = sum(1 for r in rows if r.get(field) not in (None, ""))
        if rows and got / len(rows) < share:
            fail.append(f"{field} present on only {got}/{len(rows)} rows "
                        f"(floor {share:.0%})")

    skus = [r.get("sku") for r in rows]
    if len(set(skus)) != len(skus):
        fail.append(f"{len(skus) - len(set(skus))} duplicate sku(s)")
    bad_sku = [s for s in skus if not re.fullmatch(r"\d+(?:ha)?", s or "")]
    if bad_sku:
        fail.append(f"{len(bad_sku)} sku(s) are not a Vrbo property id: "
                    f"{bad_sku[:3]}")

    # Ratings are out of TEN here, with the scale beside them.
    bad_rating = [r.get("sku") for r in rows if r.get("rating") is not None
                  and not (0 <= r["rating"] <= 10 and r.get("rating_scale") == 10)]
    if bad_rating:
        fail.append(f"{len(bad_rating)} rating(s) outside 0-10 or without a "
                    f"rating_scale of 10: {bad_rating[:3]}")

    # A currency describes a price; null without one, USD on vrbo.com.
    currencies = {r.get("currency") for r in rows if r.get("currency")}
    if currencies - {"USD"}:
        fail.append(f"vrbo.com rows carry a currency other than USD "
                    f"({sorted(currencies)})")
    mislabelled = [r.get("sku") for r in rows
                   if r.get("price") is None and r.get("currency")]
    if mislabelled:
        fail.append(f"{len(mislabelled)} row(s) carry a currency with no "
                    f"price: {mislabelled[:3]}")
    sources = {r.get("price_source") for r in rows if r.get("price_source")}
    if sources - {"card", "card-a11y"}:
        fail.append(f"listing rows carry price_source {sorted(sources)}")

    # The URL pins startDate/endDate, and a card then quotes no window of its
    # own (measured null on 50 of 50). A populated stay_dates means the site
    # ignored the dates and the prices are for different nights.
    dated = [r.get("sku") for r in rows if r.get("stay_dates")]
    if dated:
        fail.append(f"{len(dated)} row(s) carry stay_dates on a search with "
                    f"pinned dates: {dated[:3]}")
    if any(r.get("category") is not None for r in rows):
        fail.append("listing rows carry `category`, which only a property "
                    "page publishes")

    pairs = [(r.get("page"), r.get("position")) for r in rows]
    if len(set(pairs)) != len(pairs):
        fail.append(f"{len(pairs) - len(set(pairs))} row(s) share a "
                    f"page+position pair")

    # The run itself: complete, every page, and the grid actually scrolled.
    if meta.get("mode") != "listing":
        fail.append(f"run mode is {meta.get('mode')!r}, not 'listing'")
    if meta.get("status") != "complete":
        fail.append(f"run status is {meta.get('status')!r} "
                    f"(stop_reason={meta.get('stop_reason')!r}), not complete")
    if meta.get("cards_missing"):
        fail.append(f"the page's own counter says cards never loaded: "
                    f"{meta.get('cards_missing')}")
    if not meta.get("scroll"):
        fail.append("the sidecar records no scroll trace: the results "
                    "container selector has most likely moved")
    if args.pages > 1:
        pages_seen = {r.get("page") for r in rows}
        if len(pages_seen) < args.pages:
            fail.append(f"rows came from only page(s) {sorted(pages_seen)}: "
                        f"the next-button walk did not advance")
        if meta.get("pages_completed", 0) < args.pages:
            fail.append(f"pagination stopped after "
                        f"{meta.get('pages_completed')} of {args.pages} pages "
                        f"(stop_reason={meta.get('stop_reason')!r})")

    priced = sum(1 for r in rows if r.get("price") is not None)
    for line in fail:
        print(f"::error::{line}")
    print(f"[canary] {len(rows)} rows, {priced} priced, "
          f"status={meta.get('status')}, "
          f"pages={meta.get('pages_completed')}/{args.pages}, "
          f"results_total={meta.get('results_total')}, "
          f"currency={sorted(currencies)}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
