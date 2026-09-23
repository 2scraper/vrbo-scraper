#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on `sku` —
the identifier the README already tells people to diff on for price
monitoring and assortment tracking, but that nothing in this repo actually
computed.

    python3 diff_runs.py --old orlando.2026-09-01.json \\
                          --new orlando.2026-09-07.json

Typical use is a scheduled re-run of one of the scraper engines, kept
under a dated filename, diffed against the previous one:

    python3 playwright_scraper.py --url "$URL" --out "orlando_$(date +%F)"
    python3 diff_runs.py --old "$(ls -t orlando_*.json | sed -n 2p)" \\
                          --new "orlando_$(date +%F).json" --out diff.json

The main buckets, each keyed on sku:

  added          — sku present in --new, absent from --old
  removed        — sku present in --old, absent from --new (delisted, or just
                   off this particular search run)
  changed        — sku present in both, with a different value in one of
                   TRACKED_FIELDS (price, currency, rating, review_count,
                   property_type, bedrooms, beds, title)
  stay_changed   — a price move that came with a moved `stay_dates`: the
                   two runs quoted different nights, so the prices are not
                   comparable (see the README's first surprise)
  source_changed — sku present in both with a different price, but also a
                   different price_source: one run read the rendered amount
                   (`card`) and the other the screen-reader sentence
                   (`card-a11y`), or one was a property page, so the two are
                   not comparable on price. Reported separately because this
                   says something about our own two snapshots, not about the
                   site — and --fail-on-change deliberately ignores it.

A product this project's parser could not recover a sku for (None) cannot be
matched across runs at all, so it is counted and reported separately rather
than silently folded into "added"/"removed", which would be wrong on its face.
"""

import argparse
import json
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple

from output_writer import UNIQUE_BY_SKU_MODES

# No `original_price` / `discount_pct` here, because this site has neither:
# 0 strike nodes across 218 cards, 6 captures and 4 storefronts, so the
# columns do not exist on `Product` either (see output_writer's docstring).
#
# `title` IS tracked, unusually for this family: a host renaming a listing is
# a real event on a rental site and there is no other column that would show
# it.
TRACKED_FIELDS = ("price", "currency", "rating", "review_count",
                  "property_type", "bedrooms", "beds", "title")

# The subset of TRACKED_FIELDS whose comparability depends on the two runs
# having read the same node AND quoted the same stay — see diff_products.
PRICE_FIELDS = ("price",)


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(products: List[dict]) -> Tuple[Dict[str, dict], int]:
    indexed = {}
    unmatchable = 0
    for p in products:
        sku = p.get("sku")
        if sku is None:
            unmatchable += 1
            continue
        # A run's own output can already hold a duplicate sku (two rows in the
        # same category, or a rerun of dedupe_by_sku's job on older output
        # written before it existed) — keep the first and count the rest as
        # unmatchable rather than letting one clobber the other silently.
        if sku in indexed:
            unmatchable += 1
            continue
        indexed[sku] = p
    return indexed, unmatchable


def _within_tolerance(before: dict, after: dict, changes: dict,
                      tolerance_pct: float) -> bool:
    """True if every differing price field moved by less than `tolerance_pct`.

    Inherited from this family rather than earned here, and said plainly
    because the alternative is a comment inventing a reason. A sibling repo
    needs it: that site converts prices for a cross-border visitor and the
    exchange rate ticks between two runs of the same command. NO EQUIVALENT
    VRBO BEHAVIOUR WAS MEASURED — each storefront quotes its own currency
    and quotes it directly (USD on vrbo.com, EUR on fewo-direkt.de, AUD on
    stayz.com.au), and a diff across two storefronts is refused outright, so
    a comparable pair of runs holds no conversion at all.

    What DOES move here is which NIGHT was quoted, and a tolerance is the
    wrong instrument for that: a different stay is not a small drift but a
    different product. The `stay_changed` bucket in `diff_products` is the
    right one, and it is exact rather than approximate.

    So the flag stays available and DEFAULTS TO ZERO, which makes it inert
    unless someone deliberately asks for it. Set it to something non-zero
    only with a reason you can state; a price monitor that silently swallows
    small moves is worse than one that cries wolf.

    A move is judged on the LARGEST relative change among the price fields,
    so a genuine 0.5% cut is not hidden by a 0.04% tolerance applied
    field-by-field.
    """
    if tolerance_pct <= 0:
        return False
    for field in PRICE_FIELDS:
        if field not in changes:
            continue
        was, now = before.get(field), after.get(field)
        if not isinstance(was, (int, float)) or not isinstance(now, (int, float)):
            return False  # a None appearing or disappearing is a real change
        if was == 0:
            return False
        if abs(now - was) / abs(was) * 100.0 > tolerance_pct:
            return False
    return True


def diff_products(old: List[dict], new: List[dict],
                  price_tolerance_pct: float = 0.0) -> dict:
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    changed, source_changed, within_tolerance, lifecycle = [], [], [], []
    stay_changed = []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in TRACKED_FIELDS
            if before.get(field) != after.get(field)
        }
        if not field_changes:
            continue

        # THE STAY MOVED, which is not a price change — and on this site
        # this is the bucket that matters most.
        #
        # With no dates in the URL, Vrbo quotes every property its OWN
        # cheapest one-night stay: three cards in one measured load carried
        # Sep 28-29, Sep 16-17 and Sep 24-25. Run the same search tomorrow
        # and those windows have moved, so the amount beside them moves too —
        # and reporting that as a repricing would make every overnight diff
        # of a dateless search look like the whole catalogue changed its
        # mind. `--fail-on-change` ignores this bucket for the same reason it
        # ignores `source_changed`: it says something about which night was
        # quoted, not about what the host charges.
        #
        # Pin `startDate` and `endDate` in the URL and this bucket goes
        # empty, which is the point: a price monitor on this site wants a
        # fixed stay.
        stays = (before.get("stay_dates"), after.get("stay_dates"))
        if (stays[0] != stays[1] and stays[0] is not None
                and any(f in field_changes for f in PRICE_FIELDS)):
            price_part = {f: v for f, v in field_changes.items() if f in PRICE_FIELDS}
            other_part = {f: v for f, v in field_changes.items() if f not in PRICE_FIELDS}
            stay_changed.append({
                "sku": sku, "title": after.get("title"),
                "stay_dates": {"old": stays[0], "new": stays[1]},
                "changes": price_part,
            })
            field_changes = other_part
            if not field_changes:
                continue

        # A row whose price_source differs between runs is not comparable on
        # price: here that means one run read the rendered amount ("card")
        # and the other fell back to the screen-reader sentence
        # ("card-a11y"), or one run was a listing and the other a property
        # page. The figures should agree, and when they do not, the
        # difference is in how OUR two snapshots rendered, not in what the
        # host charges. Non-price fields still compare fine.
        sources = (before.get("price_source"), after.get("price_source"))
        if sources[0] != sources[1] and any(f in field_changes for f in PRICE_FIELDS):
            price_part = {f: v for f, v in field_changes.items() if f in PRICE_FIELDS}
            other_part = {f: v for f, v in field_changes.items() if f not in PRICE_FIELDS}
            source_changed.append({
                "sku": sku, "title": after.get("title"),
                "price_source": {"old": sources[0], "new": sources[1]},
                "changes": price_part,
            })
            field_changes = other_part
            if not field_changes:
                continue

        # There is no lifecycle bucket on this site, and its absence is a
        # measurement rather than an omission. A sibling repo needs one
        # because an auction closing moves `bid_kind` and the amount beside
        # it in one event; a Vrbo listing has no such state machine — a
        # property is simply listed at a nightly rate for a stay. The
        # `lifecycle` key is still emitted, always empty, so a consumer
        # written against the family's diff shape does not have to branch.
        # An FX tick rather than a price change — see _within_tolerance. Only
        # when the ONLY differences are price fields: a currency or
        # rating change alongside is a real change whatever the size of the move.
        if (all(f in PRICE_FIELDS for f in field_changes)
                and _within_tolerance(before, after, field_changes,
                                      price_tolerance_pct)):
            within_tolerance.append({"sku": sku, "title": after.get("title"),
                                     "changes": field_changes})
            continue

        changed.append({"sku": sku, "title": after.get("title"),
                        "changes": field_changes})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "stay_changed": stay_changed,
        "source_changed": source_changed,
        "within_tolerance": within_tolerance,
        "lifecycle": lifecycle,
        "unmatchable_old": old_unmatchable,
        "unmatchable_new": new_unmatchable,
    }


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed, "
          f"{len(result['source_changed'])} not comparable on price, "
          f"{len(result.get('within_tolerance', []))} within the price "
          f"tolerance, {len(result.get('stay_changed', []))} quoted for a "
          f"different stay.")
    for p in result["added"]:
        print(f"  + {p.get('sku')}  {p.get('title')}  {p.get('price')} {p.get('currency')}")
    for p in result["removed"]:
        print(f"  - {p.get('sku')}  {p.get('title')}  {p.get('price')} {p.get('currency')}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result.get("within_tolerance", []):
        moves = ", ".join(
            f"{f}: {v['old']} -> {v['new']}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {moves}  [within --price-"
              f"tolerance-pct: an exchange-rate tick, not a price change]")
    for c in result.get("stay_changed", []):
        stay = c["stay_dates"]
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  * {c['sku']}  {c['title']}  {deltas}  "
              f"[stay_dates {stay['old']!r} -> {stay['new']!r}: the site "
              f"quoted a different night, so this is not a repricing. Pin "
              f"startDate and endDate in the URL to stop it]")
    for c in result["source_changed"]:
        src = c["price_source"]
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}" for f, v in c["changes"].items())
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[price_source {src['old']!r} -> {src['new']!r}: the two runs "
              f"rendered differently, so this is not a site-side price change]")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str) -> Tuple[Optional[str], Optional[dict]]:
    """Read the `<out>.meta.json` sidecar beside a run's JSON output.

    Returns (status, meta), or (None, None) when there is no sidecar — which
    is the normal case for output written before run metadata existed, or by
    `scraper_api_client.py` (single fetch, no pagination to cut short).
    """
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


def _check_comparable(args) -> bool:
    """Refuse an assortment diff between runs that are not both complete.

    This is the failure mode the sidecar exists for: a run cut short on page
    3 of 10 is missing every product on pages 4-10, and diffing it against
    yesterday's full run reports all of them as `removed` — reading as "these
    products were delisted" when in fact they were simply never fetched.
    Prices of the SKUs both runs DID see are still comparable, which is why
    this is a refusal with a --force escape hatch rather than a hard error.
    """
    problems = []
    modes = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        if status is None:
            continue  # no sidecar: nothing to check, see _run_status
        mode = (meta or {}).get("mode")
        if mode:
            modes[label] = mode
        if mode and mode not in UNIQUE_BY_SKU_MODES:
            # This tool's whole premise is one row per `sku`, diffed on
            # price. A mode that produces many rows per sku would give a diff
            # whose every line is an artefact of two rows sharing an id, so
            # it is refused outright rather than answered. Both of this
            # repo's current modes qualify; the check is here so that adding
            # one that does not is caught rather than discovered.
            problems.append(
                f"{label} ({path}) is a {mode!r} run, which is not one row "
                f"per sku. This tool diffs one row per sku on price, so there "
                f"is nothing here it can compare.")
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(set(modes.values())) > 1:
        problems.append(
            f"the two runs are different modes ({modes}). A listing row and a "
            f"detail row carry different fields, so `added`/`removed` would "
            f"describe the mode change rather than the catalogue.")

    # A CURRENCY MISMATCH, which on this site is entirely possible.
    #
    # Vrbo runs five storefronts and they do NOT share a currency: vrbo.com
    # quotes USD, fewo-direkt.de and abritel.fr EUR, stayz.com.au AUD,
    # bookabach.co.nz NZD — measured on a live run of each. A diff of one
    # against another would report every row's price as changed, and every
    # row's sku as added and removed besides, since the catalogues are
    # different too.
    #
    # `source` catches the ordinary case and is checked below; this check
    # catches the one `source` cannot — a single run holding two currencies,
    # which means it was redirected mid-way and its own prices are not
    # comparable with each other, let alone with another run's.
    currencies = {}
    sources = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        try:
            rows = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        seen = {r.get("currency") for r in rows if r.get("currency")}
        if len(seen) == 1:
            currencies[label] = seen.pop()
        elif len(seen) > 1:
            problems.append(
                f"{label} ({path}) holds more than one currency ({sorted(seen)}) "
                f"— that run was redirected mid-way and its own prices are not "
                f"comparable with each other, let alone with another run's.")
        storefronts = {r.get("source") for r in rows if r.get("source")}
        if len(storefronts) == 1:
            sources[label] = storefronts.pop()
        elif len(storefronts) > 1:
            problems.append(
                f"{label} ({path}) holds rows from more than one storefront "
                f"({sorted(storefronts)}).")
    if len(set(currencies.values())) > 1:
        problems.append(
            f"the two runs quote different currencies ({currencies}). Vrbo's "
            f"storefronts do not share one — USD on vrbo.com, EUR on "
            f"fewo-direkt.de and abritel.fr, AUD on stayz.com.au, NZD on "
            f"bookabach.co.nz — so every row's price here is incomparable.")
    if len(set(sources.values())) > 1:
        problems.append(
            f"the two runs are different storefronts ({sources}). They are "
            f"different catalogues in different currencies, so `added` and "
            f"`removed` would describe the storefront change rather than "
            f"anything about the properties.")

    if not problems:
        return True

    # A generic headline, because the reasons below are no longer only about
    # completeness: a mode mismatch and a reviews run are refused too, and a
    # message naming the wrong reason sends the reader looking in the wrong
    # place.
    print("[!] Refusing to diff these two runs:")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the incomplete side, or pass --force to compare anyway "
          "(added/removed will include products that were simply never "
          "fetched).")
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two vrbo-scraper JSON outputs by sku.")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--price-tolerance-pct", type=float, default=0.0,
                   metavar="PCT",
                   help="Treat a price move smaller than PCT%% as an exchange-"
                        "rate tick rather than a price change: reported "
                        "separately and ignored by --fail-on-change. Default 0 "
                        "(report every cent), which is what a Vrbo "
                        "run wants: the site quotes IDR to every visitor, so "
                        "there is no conversion drift to absorb. The flag is "
                        "inherited from this scraper family; set it non-zero "
                        "only with a reason you can state.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when a run's .meta.json says it was partial "
                        "or failed. Products never fetched by the short run will "
                        "appear as added/removed.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2

    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2

    result = diff_products(old, new, price_tolerance_pct=args.price_tolerance_pct)
    _print_summary(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    # Neither `source_changed` nor `within_tolerance` is a reason to fail.
    # The first means our own two snapshots rendered differently; the second
    # means an exchange rate moved. Neither says anything about the site, and
    # alerting on either would train whoever reads the alert to ignore it.
    # Neither `stay_changed` nor `source_changed` nor `within_tolerance` is
    # a reason to fail — see their comments above.
    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
