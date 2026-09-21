"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Two modes, one row shape
------------------------
    --mode listing    a Vrbo search grid -> Product, one row per card
    --mode property   one /pdp/lo/{id} detail page -> Product, with the
                      address and the breadcrumb location populated and the
                      page/position pair null

Both modes yield the SAME class, because a property page is not a different
KIND of thing from a card — it is the same property described more fully. So
there is no second dataclass here (a sibling repo needs one for reviews; this
one does not), and `diff_runs.py` can compare a listing run against a
property run on the columns both populate.

Four family columns are NOT here, and each absence is a measurement rather
than an oversight (§9: a column that is null on every row of every run should
not exist, and removing it needs the number written down):

    original_price   0 strike nodes across 218 cards, 6 captures and 4
    discount_pct     storefronts — `uitk-text-line-through`, `strikethrough`,
                     "was $", "% off": zero occurrences of every one of them.
                     This site has no discount chain on a listing card, so
                     §4's tile-price overlay is NOT ported: dead code that
                     looks load-bearing is worse than no code.
    brand            A property has no manufacturer and a card publishes no
                     property-manager name. The nearest real value is the
                     property TYPE, which has its own column and would be
                     lying under this name.
    in_stock         A search result is bookable for the dates the card
                     quotes, so a `True` here would be an inference dressed
                     up as a reading. The dates themselves are a column.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The storefront a row came from. Unlike a sibling repo's single storefront,
# this one genuinely varies: Vrbo runs six of them (vrbo.com, fewo-direkt.de,
# abritel.fr, bookabach.co.nz, stayz.com.au, and vrbo.com/en-gb), all on the
# same Expedia front end and all serving the identical card markup. A Berlin
# rental is on fewo-direkt.de and nowhere else, so this column is the only
# thing that says which catalogue a row is from. `product_parser.source_of`
# fills it from the URL; this is the fallback for a row built without one.
SOURCE_DEFAULT = "vrbo.com"


@dataclass
class Product:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    # The property id in the URL's path, verbatim. Three id spaces share one
    # grid — 18 `/pdp/lo/N`, 25 `/N` and 7 `/Nha` across 50 measured cards —
    # and the path's last segment is what the site's own link commits to in
    # all three. NOT `expediaPropertyId`, which is a DIFFERENT number on a
    # `/N` card (path 2430840 against expediaPropertyId 70477069) and has its
    # own column below.
    sku: Optional[str] = None
    title: Optional[str] = None
    # The nightly price as the card renders it. Read the stay_dates column
    # before comparing two of these: with no dates in the URL the site quotes
    # every property its OWN cheapest one-night stay, so three cards in one
    # load carried Sep 28-29, Sep 16-17 and Sep 24-25. Those numbers are not
    # comparable to each other, and a row will appear to change price between
    # runs when all that moved was the date.
    price: Optional[float] = None
    # From the page's own `"currency":"USD"` config where it states one — a
    # written ISO code, which is a fact rather than a symbol read off a tile
    # (§4). Measured USD on vrbo.com and EUR on fewo-direkt.de. Falls back to
    # the symbol beside the amount, and is null rather than a defaulted
    # "USD" when neither is available.
    currency: Optional[str] = None
    # Out of TEN on this site, not out of five. 49 of 50 cards carry one; the
    # card without a rating is a property with no reviews yet, which is a
    # fact about the property rather than a parsing failure. The scale rides
    # in its own column beside it (see rating_scale) because this family's
    # other repos publish a five-point rating under this same name, and a
    # consumer comparing 8.6 against 4.3 without the scale is comparing
    # nothing.
    rating: Optional[float] = None
    # 49 of 50, from the card's screen-reader string — `(1,299 reviews)` on
    # vrbo.com and `(1.614 bewertungen)` on fewo-direkt.de. One of those is
    # 1299 and the other 1614; `re.search(r"\d+", ...)` returns 1 for the
    # second, which is the amazon-scraper review-count bug (§10) in a
    # different costume.
    review_count: Optional[int] = None
    # Sparse ON PURPOSE, and the reason is unusual: 41 of 50 cards carry no
    # `<img>` element at all, because the gallery is not mounted below the
    # fold. So this is populated for the cards that were on screen and null
    # for the rest, and it is recognised POSITIVELY by the media host so a
    # future placeholder cannot fill the column with something that is not an
    # image (§4).
    image_url: Optional[str] = None
    # The location breadcrumb, and only in --mode property: a detail page's
    # `BreadcrumbList` JSON-LD gives
    # "Home / Vacation Rentals / United States of America / Florida /
    # Orange County / Orlando". A search card publishes no breadcrumb, so
    # this is null on every listing row.
    category: Optional[str] = None
    # WHICH node the price was read from:
    #   "card"         the rendered amount on a search card
    #   "card-a11y"    that card's screen-reader sentence, where the rendered
    #                  node could not be read
    #   "detail"       the detail page's own price block
    #   "detail-a11y"  the same, from its screen-reader sentence
    # There is no structured price to reconcile against on ANY page kind of
    # this site — a detail page's two JSON-LD blocks are a BreadcrumbList and
    # an FAQPage, neither of them a Product — so this column records which DOM
    # node was read rather than which of two views agreed (§4). diff_runs.py
    # reports a price change that comes with a price_source change as
    # `source_changed`, not `changed`.
    price_source: Optional[str] = None
    # Which listing page this row came from (1-based) and its place in that
    # page as the site ordered it. Without `page`, `position` is ambiguous —
    # it restarts at 1 on every page, and a sibling repo shipped 60 of 119
    # rows silently claiming a position another row already held (§18). Both
    # null in --mode property, where there is no page.
    page: Optional[int] = None
    position: Optional[int] = None

    # ---- Vrbo-specific, appended after the family prefix (§9) ----
    # What `rating` is out of, read from the card's own screen-reader string
    # ("8.6 out of 10", "8,0 von 10") rather than hardcoded — so if the site
    # ever changes scale the column says so instead of the numbers silently
    # meaning something else.
    rating_scale: Optional[float] = None
    # Expedia's own id for the property, from the card's query string. The
    # SAME number as `sku` on a `/pdp/lo/N` card and a different one on a
    # `/N` card, which is exactly why both are kept.
    expedia_property_id: Optional[str] = None
    # "Aparthotel", "Condo", "Guesthouse", "Cottage" — the first segment of
    # the card's summary line, and a real value in every locale.
    property_type: Optional[str] = None
    # That line verbatim: `Aparthotel \xa0 1 bedroom \xa0 2 beds`, or just
    # `Aparthotel` on a hotel card. Kept beside the split columns below
    # because the split is POSITIONAL — the words around the numbers are
    # per-locale — and keeping the raw line is what makes a wrong split
    # visible rather than silent.
    property_summary: Optional[str] = None
    bedrooms: Optional[int] = None
    beds: Optional[int] = None
    # Whatever the card's "featured message" slot holds, verbatim, and on a
    # property page the street address instead.
    #
    # It is NOT called `neighbourhood`, and that name was tried and dropped
    # on the evidence: the slot really does carry a neighbourhood most of the
    # time ("Within Florida Center", "In Mitte"), but the same element also
    # produced "Kissimmee, 16.3 mi from Orlando" and "10 Min. Fahrt zum
    # Strand" in the measured fixtures. A column named for a neighbourhood
    # that is sometimes a driving time is a column that lies on the rows
    # where it matters most.
    #
    # Verbatim including the preposition: stripping "Within"/"In" would need
    # a phrase list per storefront, and a wrong strip is worse than a
    # preposition.
    location_note: Optional[str] = None
    # "Premier Host" and whatever joins it. A list, so CSV joins it with
    # " | " and JSON keeps the structure.
    badges: List[str] = field(default_factory=list)
    # Three amenity highlights, present on 18 of 18 cards in one capture and
    # 0 of 50 in another of the SAME URL minutes later — an A/B variant of
    # the card rather than a parsing failure. Kept because when it is present
    # it is present for every row.
    amenities: List[str] = field(default_factory=list)
    # The stay the price is quoted for, verbatim ("Sep 28 - Sep 29",
    # "30. Sept.-1. Okt."). Deliberately NOT parsed into two dates: the
    # storefronts write them six different ways and none of them states a
    # year, so any conversion would be this machine's calendar presented as
    # the site's fact. THE column to read before trusting a price diff.
    stay_dates: Optional[str] = None
    # The rest of the price block verbatim — "for 1 night", "All fees
    # included", "inkl. Steuern & Gebühren". A boolean would need a phrase
    # list per storefront; the text costs nothing and cannot be wrong.
    price_note: Optional[str] = None
    # Which kind of page this row came off: search or property. Recorded
    # because the repo reads more than one kind and the mode is no longer
    # implied by the source (§9).
    listing_kind: Optional[str] = None


ROW_CLASS_BY_MODE = {"listing": Product, "property": Product}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. Both of this repo's modes qualify: a listing page
# names each product once, and a product page IS one product.
UNIQUE_BY_SKU_MODES = ("listing", "property")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. On this site a healthy
    two-page run drops NOTHING: page 1 and page 2 of one Orlando search
    shared 0 of 50 titles, measured. So any non-zero drop count here is worth
    reading — the likeliest cause is a next-press that did not take and
    re-parsed the page it was already on.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    Both of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On this site this code specifically does NOT cover the three ways to get a
# real page with no products on it: a `/p/<slug>` discovery hub, which
# answers 200 with banners and carousels and no grid; a search whose query
# matches nothing ("Oops, produk nggak ditemukan"); and one page past the
# end of a category listing. All three are EXIT_NO_PRODUCTS — the request
# was served exactly as asked and simply has no products on it. Reporting
# any of them as blocked would send a user hunting for a proxy problem that
# does not exist.
#
# What EXIT_BLOCKED means here is unusually literal: this site refuses a
# address it has scored NOTHING at all. No status code, no interstitial, no
# vendor marker — the HTTP/2 stream is reset and the run sees a connection
# error rather than a page.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are both recorded, and on this site BOTH of them
    genuinely vary. `mode`, because a listing row and a property row populate
    different columns: `category` is null on every listing row and populated
    on every property row, so diffing one against the other would report the
    column as emptied. `source`, because Vrbo runs six storefronts and a row
    from fewo-direkt.de is priced in EUR against vrbo.com's USD — diffing
    those would report every price on the page as changed. diff_runs.py
    refuses a pair whose modes or sources differ.

    `extra` carries facts about the run that are not about any single row.
    This repo puts the listing's own counter there: `results_total`,
    `results_total_is_floor` and `cards_missing`, so a consumer can see that
    the site said "1 - 50 of 300+" and the run merged 40 rows off that page
    WITHOUT re-reading the HTML. That gap is arithmetic rather than a
    threshold (§8), and one measured run had it: page 2 read "51 - 100 of
    300+" and yielded 40 cards.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" beside it, and on
# this site the ordering between them is not a preference — it is the only
# thing that works.
#
# Vrbo publishes no `link[rel=next]`, no `a[rel=next]`, no `<link
# rel=canonical>` and no numbered anchors anywhere on a listing page. Its
# next control is a `<button>` that fires a GraphQL POST and leaves the
# address bar untouched, and the URL conventions that would let a run
# address page 2 do not fail when you try them — they silently return page 1
# (measured: `&startIndex=50` and `&page=2` both answered HTTP 200 with the
# counter still reading "1 - 50 of 300+" and the same first cards). So a run
# that trusted a built URL would find no new sku, call the listing
# exhausted, and report COMPLETE holding a sixth of the catalogue (§18).
#
# "no new products" is therefore the data-side termination condition, and
# "pagination_exhausted" means the site's own button was gone or disabled —
# a property of the DOM, checked second.
#
# "single_page_mode" is complete by construction: --mode property reads one
# page because one page is all there is.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
