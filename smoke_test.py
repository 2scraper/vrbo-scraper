#!/usr/bin/env python3
"""vrbo-scraper — offline smoke tests.

One file of plain functions with fixtures loaded from `fixtures_generated.json`,
no pytest required. `tests/test_smoke.py` wraps it as a single pytest test so
`pytest` works as an entry point without a second copy of the checks.

    python3 smoke_test.py

It MUST pass with no engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is REPORTED, because "skipped, engine absent" reads
exactly like a passing run. CI's engine-smoke job installs each engine in its
own venv and fails if that skip list is non-empty.

The fixtures are cut from real captures by `make_fixtures.py`, which proves
each one parses IDENTICALLY to its untrimmed original, column for column,
and scrubs the challenge page's site keys. Do not hand-edit them.

WHAT THIS SUITE IS FOR, beyond the obvious
------------------------------------------
Most of these checks exist because of a specific failure, in this repo or in
a sibling. The ones worth knowing about before you change anything:

  * `test_values_on_real_fixtures` asserts VALUES, not coverage. A column can
    be 100% populated and entirely wrong — a sibling repo shipped a
    `review_count` of 445279961 on every row of every mode while its coverage
    check said 100%. The German fixture is the one that matters here:
    `1.614 Bewertungen` is 1614, and `re.search(r"\\d+", ...)` returns 1.

  * `test_engine_parity` binds every shared-module call in every engine
    against the callee's REAL signature. Two engines in a sibling repo called
    `classify(html, url=...)` where the parameter is positional, both crashed
    on their first fetch, and nothing short of a live run saw it.

  * `test_throttle_is_not_completion` pins the bug this repo's own first live
    run found: a page turn refused by the site's rate limiter must NOT be
    reported as the end of the listing, because `pagination_exhausted` is a
    COMPLETE stop reason and a throttled run would say "complete" while
    holding one page of six.
"""

import ast
import contextlib
import csv as csv_module
import inspect
import io
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, fields

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import captcha_solver
import env_config
import page_flow
import product_parser
from diff_runs import diff_products
from output_writer import (Product, save, finish_run, write_csv, run_meta,
                           dedupe_by_key, dedupe_by_sku, ROW_CLASS_BY_MODE,
                           UNIQUE_BY_SKU_MODES, COMPLETE_STOP_REASONS,
                           EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL,
                           EXIT_API_ERROR, LIST_CSV_SEPARATOR, SOURCE_DEFAULT)
from product_parser import (parse_products, parse_property_page, SELECTORS,
                            HOSTS, HOST_CURRENCY, ISO_CURRENCIES, PAGE_CAP,
                            NEXT_PAGE_SELECTOR, PAGINATES_BY_URL,
                            NO_RESULTS_MARKERS, CHALLENGE_MARKERS,
                            SOLVABLE_CHALLENGES, challenge_vendor,
                            currency_in, detect_block_marker,
                            detect_bot_challenge, detect_page_state,
                            expedia_property_id_from_url, int_in,
                            is_no_results, is_supported_host, listing_kind,
                            numbers_in, page_currency, paginates_by_url,
                            price_in, prices_in, results_range, served_by_vrbo,
                            site_host, sku_from_url, source_of, strip_tracking,
                            unsupported_reason, missing_on_page)
from proxy_pool import ProxyPool, mask, to_playwright, split_credentials

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")
SHARED_MODULES = {"page_flow": page_flow, "product_parser": product_parser}

_failures = []
_total_checks = 0


def check(label, condition):
    """Print and record one check. Returns the condition so callers can
    accumulate with `ok &= check(...)`."""
    global _total_checks
    _total_checks += 1
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def _raises(fn):
    """True if `fn()` raises. Used where refusing is the correct behaviour."""
    try:
        fn()
    except Exception:
        return True
    return False


_FIXTURE_PATH = os.path.join(REPO_ROOT, "fixtures_generated.json")
if not os.path.exists(_FIXTURE_PATH):
    # Said in words rather than as a bare FileNotFoundError, because the
    # first time this happened it was not missing from the disk — it was
    # missing from the COMMIT. `.gitignore` carries a blanket `*.json` (a
    # scraper's own output is large and stale by the time anyone reads it),
    # which swallowed it silently: the whole suite was green locally and
    # every CI job died at import. `test_required_files_are_committed` now
    # catches that case directly.
    raise SystemExit(
        f"fixtures_generated.json is missing from {REPO_ROOT}.\n"
        f"If you are in a clean checkout, it should have been committed — "
        f"check that .gitignore's `*.json` rule still carries the "
        f"`!fixtures_generated.json` exception.\n"
        f"If you are regenerating fixtures, run: python3 make_fixtures.py")
with open(_FIXTURE_PATH, encoding="utf-8") as _f:
    FIXTURES = json.load(_f)
URLS = FIXTURES["_URLS"]


def fixture(name):
    return FIXTURES[name]


def rows_of(name, page=1):
    return parse_products(FIXTURES[name], URLS[name], page=page)


def by_sku(name, page=1):
    return {r.sku: r for r in rows_of(name, page)}


# ---------------------------------------------------------------------------
def test_numbers_and_prices():
    group("Numbers, in six locales")
    ok = True
    # The three grouping conventions, and the no-break spaces a rendered page
    # actually uses — missing them parses `1 234 €` as 234.
    ok &= check("1,234.56 -> 1234.56", price_in("$1,234.56") == 1234.56)
    ok &= check("1.234,56 -> 1234.56", price_in("1.234,56 €") == 1234.56)
    ok &= check("NBSP grouping 1 234 € -> 1234", price_in("1 234 €") == 1234.0)
    ok &= check("narrow NBSP 1 234 € -> 1234", price_in("1 234 €") == 1234.0)
    # Exactly three trailing digits is a THOUSANDS grouping: no currency here
    # has a three-digit subunit.
    ok &= check("$1,234 -> 1234 (grouping, not cents)", price_in("$1,234") == 1234.0)
    ok &= check("8,0 -> 8.0 (two digits is a decimal)", numbers_in("8,0")[0] == 8.0)

    # THE German trap. `int("1.614")` raises and `re.search(r"\d+", ...)`
    # returns 1.
    ok &= check("(1.614 bewertungen) -> 1614", int_in("(1.614 bewertungen)") == 1614)
    ok &= check("(1,299 reviews) -> 1299", int_in("(1,299 reviews)") == 1299)
    ok &= check("(677 Bewertungen) -> 677", int_in("(677 Bewertungen)") == 677)

    # A percentage must be removed BEFORE matching: a rejected match has
    # already consumed the currency symbol, so skipping it loses the real
    # price too.
    ok &= check("-16% $250 -> 250", price_in("-16% $250") == 250.0)
    ok &= check("-%10,34 ₺ ignored for a $ price", price_in("-%10,34 $25.999") == 25999.0)

    group("Currency, in order of trust")
    # 1. The page's own ISO code is a FACT.
    ok &= check("page states USD", page_currency(fixture("LISTING_US")) == "USD")
    ok &= check("page states EUR", page_currency(fixture("LISTING_DE")) == "EUR")
    # 2. A written ISO code names itself; an allowlist, never [A-Z]{3}.
    ok &= check("a written ISO code names itself", currency_in("100 SEK") == "SEK")
    ok &= check("XXL 100 is not a currency", currency_in("XXL 100") is None)
    # 3. A prefixed symbol, longest-first so A$ is not swallowed by $.
    ok &= check("A$18 -> AUD", currency_in("A$18") == "AUD")
    ok &= check("NZ$18 -> NZD", currency_in("NZ$18") == "NZD")
    # 4. A BARE $ resolves from the HOST, never to a defaulted USD. Reading
    #    stayz.com.au's $18 as USD turns an A$18 property into an $18 one.
    ok &= check("bare $ on stayz -> AUD",
                currency_in("$18", "https://www.stayz.com.au/search") == "AUD")
    ok &= check("bare $ on bookabach -> NZD",
                currency_in("$18", "https://www.bookabach.co.nz/search") == "NZD")
    ok &= check("bare $ on vrbo.com -> USD",
                currency_in("$81", "https://www.vrbo.com/search") == "USD")
    # 5. Absent is null, never a defaulted "USD".
    ok &= check("no symbol, no host -> None", currency_in("81") is None)
    ok &= check("every HOST_CURRENCY value is a real ISO code",
                all(v in ISO_CURRENCIES for v in HOST_CURRENCY.values()))
    return ok


def test_values_on_real_fixtures():
    group("VALUES on real fixtures, not coverage")
    ok = True
    us = by_sku("LISTING_US")
    ok &= check("LISTING_US parses 4 cards", len(us) == 4)
    a = us.get("63925107")
    ok &= check("title read off the right h3 (not the gallery's)",
                a is not None and a.title == "The Point Hotel & Suites Orlando")
    ok &= check("price 81.0 USD", a is not None and a.price == 81.0 and a.currency == "USD")
    ok &= check("rating 8.6 out of TEN", a is not None and a.rating == 8.6
                and a.rating_scale == 10.0)
    ok &= check("review_count 1299", a is not None and a.review_count == 1299)
    ok &= check("property type/bedrooms/beds split positionally",
                a is not None and a.property_type == "Aparthotel"
                and a.bedrooms == 1 and a.beds == 2)
    ok &= check("stay dates kept verbatim",
                a is not None and a.stay_dates == "Sep 28 - Sep 29")
    ok &= check("price_note keeps the rest of the block verbatim",
                a is not None and a.price_note == "for 1 night | All fees included")

    # The "Premier Host" badge shares the rating badge's class, so the rating
    # is chosen by being a NUMBER rather than by position.
    b = us.get("2430840")
    ok &= check("Premier Host badge does not become the rating",
                b is not None and b.rating == 9.8 and b.badges == ["Premier Host"])
    # Two id spaces on one card: the path id is not expediaPropertyId.
    ok &= check("path sku and expediaPropertyId are kept apart",
                b is not None and b.sku == "2430840"
                and b.expedia_property_id == "70477069")

    group("The German storefront — the second-locale checks")
    de = by_sku("LISTING_DE")
    ok &= check("LISTING_DE parses 4 cards", len(de) == 4)
    d = de.get("2666640")
    ok &= check("rating from '9,0 von 10'", d is not None and d.rating == 9.0
                and d.rating_scale == 10.0)
    ok &= check("review_count from '(1.614 bewertungen)' is 1614",
                d is not None and d.review_count == 1614)
    ok &= check("price 82 EUR from '82 €'",
                d is not None and d.price == 82.0 and d.currency == "EUR")
    ok &= check("price read through data-test-id, not data-stid",
                all(r.price is not None for r in de.values()))
    ok &= check("source is the storefront, not vrbo.com",
                all(r.source == "fewo-direkt.de" for r in de.values()))
    # The fifth URL shape, which vrbo.com never uses.
    ok &= check("slug route /{slug}/p{id} yields a sku",
                de.get("5813635") is not None)
    ok &= check("every German card has a sku",
                all(r.sku for r in de.values()))

    group("The Australian storefront — the bare-$ check")
    au = by_sku("LISTING_AU")
    ok &= check("bare $ on stayz.com.au reads AUD, not USD",
                all(r.currency == "AUD" for r in au.values()))
    ok &= check("stayz prices parsed", au.get("40902") is not None
                and au["40902"].price == 209.0)

    group("Page and position — the pair, not either alone")
    p1 = rows_of("LISTING_US", page=1)
    p2 = rows_of("LISTING_US_P2", page=2)
    ok &= check("page is threaded in, not defaulted to 1",
                all(r.page == 1 for r in p1) and all(r.page == 2 for r in p2))
    ok &= check("position restarts per page", [r.position for r in p2] == [1, 2])
    pairs = [(r.page, r.position) for r in p1 + p2]
    ok &= check("page+position unique across a multi-page run",
                len(set(pairs)) == len(pairs))
    ok &= check("no sku overlap between page 1 and page 2",
                not ({r.sku for r in p1} & {r.sku for r in p2}))

    group("Columns that are sparse ON PURPOSE")
    ok &= check("image_url is recognised positively by the media host",
                all(r.image_url is None or r.image_url.startswith("https://media.")
                    for r in p1 + p2 + list(de.values())))
    # A property with no reviews yet has no rating, and that is a fact about
    # the property rather than a parsing failure.
    ok &= check("a card with no reviews has rating None, not 0",
                de["5813635"].rating is None and de["5813635"].review_count is None)

    group("A card the site renders without a price (real capture)")
    unpriced = parse_products(FIXTURES["LISTING_UNPRICED"], URLS["LISTING_UNPRICED"])
    ok &= check("one row, identified and titled",
                len(unpriced) == 1 and unpriced[0].sku == "12245666ha"
                and unpriced[0].title == "1 Full Bed | ADA/RI Shower")
    ok &= check("no price, and therefore no currency or price_source",
                unpriced[0].price is None and unpriced[0].currency is None
                and unpriced[0].price_source is None)
    ok &= check("the rest of the card still reads (rating 10 of 10, 5 reviews)",
                unpriced[0].rating == 10.0 and unpriced[0].rating_scale == 10.0
                and unpriced[0].review_count == 5)
    ok &= check("a priced card keeps its currency",
                all(r.currency == "USD" for r in rows_of("LISTING_US")
                    if r.price is not None))

    group("The property page")
    prop = parse_property_page(fixture("PROPERTY_US"), URLS["PROPERTY_US"])
    ok &= check("property mode yields exactly one row", len(prop) == 1)
    r = prop[0]
    ok &= check("street address in location_note",
                r.location_note == "7389 Universal Boulevard, Orlando, FL, 32819")
    ok &= check("category from the BreadcrumbList, property name dropped",
                r.category == ("Home / Vacation Rentals / United States of "
                               "America / Florida / Orange County / Orlando"))
    ok &= check("price_source says which node was read", r.price_source == "detail")
    ok &= check("page/position null in property mode",
                r.page is None and r.position is None)
    ok &= check("listing_kind records the page kind",
                r.listing_kind == "property"
                and all(x.listing_kind == "search" for x in p1))
    return ok


def test_urls():
    group("URL shapes — all five of them")
    ok = True
    cases = {
        "https://www.vrbo.com/pdp/lo/63925107": "63925107",
        "https://www.vrbo.com/2430840": "2430840",
        "https://www.vrbo.com/504312ha": "504312ha",
        "https://www.fewo-direkt.de/ferienwohnung-ferienhaus/p5813635": "5813635",
        "https://www.stayz.com.au/holiday-rental/p20304340": "20304340",
        "https://www.vrbo.com/en-gb/pdp/lo/123": "123",
    }
    for url, want in cases.items():
        ok &= check(f"sku of {url.split('.com')[-1].split('.de')[-1].split('.au')[-1]} "
                    f"is {want}", sku_from_url(url) == want)
    ok &= check("a search URL has no sku",
                sku_from_url("https://www.vrbo.com/search?destination=X") is None)

    group("listing_kind")
    ok &= check("search", listing_kind("https://www.vrbo.com/search?destination=X") == "search")
    ok &= check("search with a locale prefix",
                listing_kind("https://www.vrbo.com/en-gb/search?destination=X") == "search")
    ok &= check("property", listing_kind("https://www.vrbo.com/pdp/lo/1") == "property")
    ok &= check("unknown", listing_kind("https://www.vrbo.com/trips") == "unknown")

    group("Supported hosts — measured, not guessed")
    for host in HOSTS:
        ok &= check(f"{host} is supported",
                    is_supported_host(f"https://www.{host}/search?destination=X"))
    ok &= check("a bare host works too (no www.)",
                is_supported_host("https://vrbo.com/search?destination=X"))
    # Refused WITH the reason: "is not a Vrbo site" is false for homeaway.com
    # and sends the reader hunting for a typo that is not there.
    why = unsupported_reason("https://www.homeaway.com/search?destination=X")
    ok &= check("homeaway.com refused BY NAME, with the redirect as the reason",
                why is not None and "redirects to vrbo.com" in why)
    ok &= check("an unrelated host is refused",
                unsupported_reason("https://www.booking.com/x") is not None)
    ok &= check("a non-http URL is refused",
                unsupported_reason("ftp://vrbo.com/x") is not None)

    group("source_of maps a URL to its storefront")
    ok &= check("vrbo.com", source_of("https://www.vrbo.com/search") == "vrbo.com")
    ok &= check("fewo-direkt.de",
                source_of("https://www.fewo-direkt.de/search") == "fewo-direkt.de")
    ok &= check("SOURCE_DEFAULT is a real host", SOURCE_DEFAULT in HOSTS)

    group("strip_tracking — a card href carries a dozen session params")
    href = ("https://www.vrbo.com/pdp/lo/63925107?dateless=true&x_pwa=1&rfrr=HSR"
            "&pwa_ts=1789394670325&searchId=abc&adults=2&regionId=2693"
            "&expediaPropertyId=63925107&latLong=28.5%2C-81.3")
    cleaned = strip_tracking(href)
    ok &= check("pwa_ts stripped (it changes on every load)", "pwa_ts" not in cleaned)
    ok &= check("searchId stripped (per-load)", "searchId" not in cleaned)
    ok &= check("regionId kept (it identifies the search)", "regionId" in cleaned)
    ok &= check("expediaPropertyId kept (it is a column)",
                "expediaPropertyId" in cleaned)
    ok &= check("expedia id read off the query",
                expedia_property_id_from_url(href) == "63925107")
    ok &= check("two loads of the same card compare equal",
                strip_tracking(href) == strip_tracking(href.replace("1789394670325", "1789399999999")))
    return ok


def test_pagination():
    group("Pagination — a button, not an address")
    ok = True
    # There must be NO page_url() in this module. `&page=2` and
    # `&startIndex=50` were both tried against the live site and both
    # answered 200 with page 1's own cards, so a built URL would make a run
    # report COMPLETE holding a sixth of the catalogue.
    ok &= check("product_parser deliberately has no page_url()",
                not hasattr(product_parser, "page_url"))
    ok &= check("page_flow deliberately has no next_page_candidates()",
                not hasattr(page_flow, "next_page_candidates"))
    ok &= check("PAGINATES_BY_URL is False", PAGINATES_BY_URL is False)
    ok &= check("paginates_by_url is False for a search",
                paginates_by_url("https://www.vrbo.com/search?destination=X") is False)
    ok &= check("the next control is the site's own data-stid",
                "next-button" in NEXT_PAGE_SELECTOR
                and ":not([disabled])" in NEXT_PAGE_SELECTOR)

    group("Concurrency is refused WITH the reason")
    refusal = page_flow.concurrency_refusal("https://www.vrbo.com/search?destination=X")
    ok &= check("refused for a search", refusal is not None)
    ok &= check("the reason names the cause, not just 'unsupported'",
                refusal is not None and "no per-page address" in refusal)
    ok &= check("refused for a property page too",
                page_flow.concurrency_refusal("https://www.vrbo.com/pdp/lo/1") is not None)
    ok &= check("concurrency_limit is 1 everywhere",
                page_flow.concurrency_limit("https://www.vrbo.com/search?d=X") == 1)

    group("The counter is the completeness oracle")
    us = results_range(fixture("LISTING_US"))
    ok &= check("'1 - 50 of 300+' parses", (us.first, us.last, us.total) == (1, 50, 300))
    ok &= check("the '+' is recorded as a floor", us.total_is_floor is True)
    ok &= check("expected_on_page is arithmetic", us.expected_on_page == 50)
    p2 = results_range(fixture("LISTING_US_P2"))
    ok &= check("page 2 reads '51 - 100'", (p2.first, p2.last) == (51, 100))
    # The German counter is `1–50 von >300`: an EN DASH instead of a hyphen
    # and the floor marker on the other side of the number. Parsed as "the
    # integers in that text" for exactly this reason.
    de = results_range(fixture("LISTING_DE"))
    ok &= check("German '1–50 von >300' parses the same",
                (de.first, de.last, de.total) == (1, 50, 300))
    ok &= check("German '>' also reads as a floor", de.total_is_floor is True)
    ok &= check("4 parsed against a stated 50 is a gap of 46",
                missing_on_page(fixture("LISTING_US"), 4) == 46)
    ok &= check("a page with no counter reports gap None, not 0",
                page_flow.page_gap("<html><body></body></html>", 0) is None)
    ok &= check("no counter means no invented number",
                missing_on_page("<html></html>", 0) == 0)

    group("The page cap")
    ok &= check("PAGE_CAP is a real bound", isinstance(PAGE_CAP, int) and PAGE_CAP > 1)
    ok &= check("page_cap_reached fires at the cap",
                page_flow.page_cap_reached(PAGE_CAP)
                and not page_flow.page_cap_reached(PAGE_CAP - 1))
    return ok


def test_page_state():
    group("detect_page_state on real captures")
    ok = True
    ok &= check("a served grid is content",
                detect_page_state(fixture("LISTING_US"), 200, URLS["LISTING_US"]) == "content")
    ok &= check("a property page is content, NOT shell",
                detect_page_state(fixture("PROPERTY_US"), 200,
                                  URLS["PROPERTY_US"]) == "content")
    for name in ("EMPTY_EN", "EMPTY_DE", "EMPTY_FR"):
        ok &= check(f"{name} is empty, in the site's own words",
                    detect_page_state(fixture(name), 200, URLS[name]) == "empty")
        ok &= check(f"{name} is_no_results", is_no_results(fixture(name)))
    ok &= check("a good page is NOT no_results", not is_no_results(fixture("LISTING_US")))
    ok &= check("the 429 challenge handler is blocked",
                detect_page_state(fixture("BLOCK_429"), 429, URLS["BLOCK_429"]) == "blocked")
    # Chromium's own answer when a proxy is dead: 39 bytes, no markup, and
    # nothing a vendor-marker list would recognise. Only "was this built out
    # of the site's own assets?" answers correctly.
    ok &= check("a browser error page is blocked, not content",
                detect_page_state(fixture("BROWSER_ERROR"), None,
                                  URLS["BROWSER_ERROR"]) == "blocked")
    ok &= check("html=None is blocked, not a crash",
                page_flow.classify(None, None, "https://www.vrbo.com/search") == "blocked")
    # A served page with no grid and no no-results copy is STILL PAINTING,
    # and wants a wait rather than a refetch. Getting this backwards reports
    # a slow page as an empty catalogue.
    shell = ('<html lang="en"><head>'
             '<link href="https://c.travel-assets.com/a.css">'
             '<link href="https://a.travel-assets.com/b.css">'
             '<link href="https://b.travel-assets.com/c.css">'
             '</head><body><div data-stid="property-listing-results"></div></body></html>')
    ok &= check("a served, unpainted search page is shell",
                detect_page_state(shell, 200, "https://www.vrbo.com/search?d=X") == "shell")
    ok &= check("is_unpainted agrees", page_flow.is_unpainted("shell", shell))

    group("The asset threshold is never asked to tell a block from a page")
    # It cannot: the challenge handler is built out of travel-assets too. The
    # unambiguous positives are checked first, which is what keeps a leanly
    # built real page from coming back blocked (the ordering trap).
    ok &= check("a served grid references the site's assets",
                served_by_vrbo(fixture("LISTING_US")))
    ok &= check("the browser error page does not",
                not served_by_vrbo(fixture("BROWSER_ERROR")))

    group("`akamai` is NOT a marker here, and that is the point")
    # Vrbo IS fronted by Akamai and every good page loads its sensor, so a
    # marker list containing `akamai` would match an 899 KB page holding the
    # full grid.
    all_markers = " ".join(CHALLENGE_MARKERS + product_parser.BOT_CHALLENGE_MARKERS)
    ok &= check("no marker mentions akamai", "akamai" not in all_markers.lower())
    for name in ("LISTING_US", "LISTING_DE", "LISTING_AU", "PROPERTY_US"):
        ok &= check(f"no challenge marker fires on {name}",
                    detect_block_marker(fixture(name)) is None)
    return ok


def test_challenge_is_not_always_solvable():
    group("detected != blocking != paying")
    ok = True
    block = fixture("BLOCK_429")
    # Expedia's handler is a multiplexer that states its pick in the page's
    # own config rather than in markup, so the vendor is DATA and a list of
    # vendor markers cannot name it.
    ok &= check("the vendor is read out of the page's config",
                challenge_vendor(block) == "datadome-challenge")
    ok &= check("detect_block_marker names the vendor",
                detect_block_marker(block) == "datadome-challenge")
    # This repo solves reCAPTCHA and nothing else. A DataDome pick must
    # therefore be `blocked`, so no solve is attempted and nothing is
    # charged.
    ok &= check("a DataDome pick is NOT offered to the solver",
                detect_bot_challenge(block) is None)
    ok &= check("and the state is blocked, not challenge",
                detect_page_state(block, 429, URLS["BLOCK_429"]) == "blocked")
    ok &= check("blocked does not buy a solve",
                page_flow.should_solve("blocked") is False)
    ok &= check("challenge does buy one",
                page_flow.should_solve("challenge") is True)
    ok &= check("SOLVABLE_CHALLENGES names only what the solver implements",
                tuple(SOLVABLE_CHALLENGES) == ("recaptcha",))
    # A reCAPTCHA pick, on the other hand, IS worth a solve. Built by
    # substituting the vendor in the real fixture, so the surrounding markup
    # is the site's.
    recaptcha = block.replace("datadome-challenge", "recaptcha-challenge")
    ok &= check("a reCAPTCHA pick IS offered to the solver",
                detect_bot_challenge(recaptcha) == "recaptcha-challenge")
    ok &= check("and the state becomes challenge",
                detect_page_state(recaptcha, 429, URLS["BLOCK_429"]) == "challenge")

    group("block_advice says what to DO")
    advice = page_flow.block_advice(block, headless=False, has_pool=False)
    ok &= check("it does NOT blame the browser build (12 of 12 served on both)",
                "channel=chrome" not in advice and "reads the CLIENT" not in advice)
    ok &= check("it says the exit's score is noisy and the browser is not the first lever",
                "noisy" in advice and "not the first lever" in advice)
    ok &= check("it says the vendor could not be solved and nothing was charged",
                "nothing was charged" in advice)
    ok &= check("it names the rate response as clearable",
                "RATE response" in advice)
    return ok


def test_block_backoff_and_schema_version():
    group("--block-retry-delay backs off; the sidecar carries schema_version")
    ok = True
    bp = page_flow.block_pause
    ok &= check("unset keeps the historical linear wait (2, 4)",
                [bp(2.0, n) for n in (0, 1)] == [2.0, 4.0])
    ok &= check("set, it starts at the base and doubles (30, 60, 120)",
                [bp(2.0, n, 30.0) for n in (0, 1, 2)] == [30.0, 60.0, 120.0])
    for eng in ("playwright_scraper", "puppeteer_scraper", "selenium_scraper"):
        src = open(eng + ".py", encoding="utf-8").read()
        ok &= check(f"{eng} takes --block-retry-delay and routes it through block_pause",
                    '"--block-retry-delay"' in src
                    and "page_flow.block_pause(args.retry_delay, block_attempt," in src)
    import output_writer
    meta = output_writer.run_meta("complete", "completed", 1, 1, "u", "u", 1)
    ok &= check("run_meta records schema_version",
                meta.get("schema_version") == output_writer.SCHEMA_VERSION)
    ok &= check("a caller's extra cannot overwrite it",
                output_writer.run_meta("complete", "completed", 1, 1, "u", "u", 1,
                    extra={"schema_version": "9"})["schema_version"]
                == output_writer.SCHEMA_VERSION)
    ok &= check("it is a MAJOR.MINOR.PATCH string",
                re.fullmatch(r"\d+\.\d+\.\d+", output_writer.SCHEMA_VERSION) is not None)
    return ok


def test_retries_zero_is_refused():
    group("--retries 0 would never navigate, so the parser refuses it")
    ok = True
    import argparse
    ok &= check("1 is accepted", page_flow.attempts_arg("1") == 1)
    for bad in ("0", "-1", "x"):
        try:
            page_flow.attempts_arg(bad)
            raised = False
        except argparse.ArgumentTypeError:
            raised = True
        ok &= check(f"{bad!r} is refused", raised)
    import ast
    for eng in ("playwright_scraper", "puppeteer_scraper", "selenium_scraper"):
        src = open(eng + ".py", encoding="utf-8").read()
        ok &= check(f"{eng} wires --retries through attempts_arg",
                    '"--retries", type=page_flow.attempts_arg' in src)
    return ok


def test_page_flow_policy():
    group("STATE_POLICY — the triage as DATA, not three if-chains")
    ok = True
    for state in ("content", "empty", "shell", "challenge", "blocked"):
        ok &= check(f"{state} has a full policy row",
                    set(page_flow.STATE_POLICY[state]) ==
                    {"parse", "retry", "solve", "blocked"})
    ok &= check("content is parsed and not retried",
                page_flow.should_parse("content") and not page_flow.should_retry("content"))
    ok &= check("empty is a final answer, not a fault",
                not page_flow.should_parse("empty") and not page_flow.should_retry("empty"))
    ok &= check("shell is parsed after the wait, never refetched",
                page_flow.should_parse("shell") and not page_flow.should_retry("shell"))
    ok &= check("blocked counts towards exit 3",
                page_flow.counts_as_blocked("blocked")
                and not page_flow.counts_as_blocked("empty"))
    ok &= check("an unknown state falls back to the blocked row",
                page_flow.should_parse("nonsense") is False)

    group("Retrying a block DOES help here — and the engines CONSULT that")
    # A policy constant nothing reads is the same defect as dead code.
    ok &= check("RETRY_ON_BLOCKED is True on this site",
                page_flow.RETRY_ON_BLOCKED is True)
    ok &= check("the budget is non-zero", page_flow.BLOCK_RETRIES_WITHOUT_POOL > 0)
    ok &= check("a pool buys more attempts",
                page_flow.BLOCK_RETRIES_WITH_POOL >= page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    consulted = []
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        consulted.append("RETRY_ON_BLOCKED" in src)
    ok &= check("every engine reads RETRY_ON_BLOCKED", all(consulted))

    group("Readiness — a count poll, never an evaluated string")
    ok &= check("MIN_CARD_MATCHES is above 1", page_flow.MIN_CARD_MATCHES > 1)
    ok &= check("the listing anchor is the card itself",
                page_flow.ready_selector("listing") == SELECTORS["item_card"])
    ok &= check("a property page waits on its price block, not its title",
                page_flow.ready_selector("property") == SELECTORS["detail_price"])
    # A short last page can never reach the floor, so the counter lowers it.
    ok &= check("min_matches is clamped by what the page says it holds",
                page_flow.min_matches("listing", 1) == 1)
    ok &= check("and is not raised above the floor",
                page_flow.min_matches("listing", 50) == page_flow.MIN_CARD_MATCHES)
    ok &= check("expected_cards reads the counter",
                page_flow.expected_cards(fixture("LISTING_US")) == 50)

    calls = []

    def count(_sel):
        calls.append(1)
        return 0 if len(calls) < 4 else 9

    found = page_flow.wait_for_count(count, lambda ms: None, "x", 5, 5_000)
    ok &= check("wait_for_count returns the count it reached", found == 9)
    ok &= check("a wait that never satisfies still returns, bounded",
                page_flow.wait_for_count(lambda s: 0, lambda ms: None, "x", 5, 400) == 0)
    return ok


def test_scroll_loop():
    group("The scroll — the inner container, and THREE stable rounds")
    ok = True
    # The window scroll is a no-op on this site and looks exactly like a
    # working one, which is why the container is named in SELECTORS and every
    # engine scrolls THAT.
    ok &= check("a scroll container is named",
                SELECTORS["scroll_container"] == ".scrollable-result-section")
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        ok &= check(f"{engine} scrolls the results container",
                    'SELECTORS["scroll_container"]' in src)
        ok &= check(f"{engine} falls back to the window if it is absent",
                    "document.body.scrollHeight" in src)

    # A pause is not an ending. This is the loop that settled at 19 cards
    # against a counter saying 50 on the first live run, because the next
    # batch was in flight behind a throttled /graphql POST.
    counts = [3, 3, 3, 3, 3, 50, 50, 50, 50, 50, 50, 50]
    state = {"i": 0}

    def count(_sel):
        i = min(state["i"], len(counts) - 1)
        return counts[i]

    def scroll():
        state["i"] += 1

    reached = page_flow.scroll_until_settled(
        count, scroll, lambda: 1000, lambda ms: None, target=50)
    ok &= check("it waits through a pause when the counter says more are owed",
                reached == 50)
    ok &= check("more patience below a known target than above it",
                page_flow.SCROLL_STABLE_ROUNDS_BELOW_TARGET
                > page_flow.SCROLL_STABLE_ROUNDS)
    ok &= check("three stable rounds, not one", page_flow.SCROLL_STABLE_ROUNDS >= 3)

    # With no counter, the stable-round heuristic IS the termination
    # condition, and it must still terminate.
    rounds = {"n": 0}

    def scroll2():
        rounds["n"] += 1

    reached2 = page_flow.scroll_until_settled(
        lambda s: 7, scroll2, lambda: 500, lambda ms: None, target=None)
    ok &= check("with no counter it settles and stops", reached2 == 7)
    ok &= check("and does not spend the whole budget",
                rounds["n"] <= page_flow.SCROLL_STABLE_ROUNDS + 1)

    # A page that grows forever must not hang the run.
    forever = {"n": 0}

    def count3(_sel):
        forever["n"] += 1
        return forever["n"]

    page_flow.scroll_until_settled(count3, lambda: None, lambda: forever["n"] * 10,
                                   lambda ms: None, target=None)
    ok &= check("a forever-growing page is bounded by the round budget",
                forever["n"] <= page_flow.SCROLL_MAX_ROUNDS * 2 + 2)
    return ok


def test_throttle_is_not_completion():
    group("A throttled page turn is PARTIAL, not the end of the listing")
    ok = True
    # The bug this repo's own first live run found. `pagination_exhausted` is
    # a COMPLETE stop reason, so returning it for a refused turn would make a
    # rate-limited run report "complete" while holding one page of six.
    ok &= check("the three outcomes are distinct values",
                len({page_flow.ADVANCED, page_flow.NO_BUTTON,
                     page_flow.NO_TURNOVER}) == 3)

    # No button at all: the listing genuinely ran out.
    ok &= check("no button -> NO_BUTTON",
                page_flow.advance_to_next_page(
                    lambda: False, lambda: None, lambda s: 0,
                    lambda ms: None) == page_flow.NO_BUTTON)

    # Pressed, and the grid never came back.
    ok &= check("pressed but never turned over -> NO_TURNOVER",
                page_flow.advance_to_next_page(
                    lambda: True,
                    lambda: "/pdp/lo/1?x=1",
                    lambda s: 3,
                    lambda ms: None,
                    timeout_ms=3_000) == page_flow.NO_TURNOVER)

    # A real turn: the first card's sku changes. The count going briefly to
    # zero mid-transition is normal and must not read as a failure.
    polls = {"n": 0}

    def href():
        # Before the press, then once the transition has settled. The card
        # count going to zero mid-transition is normal (the results
        # container is rebuilt) and must not read as a failure.
        return "/pdp/lo/1?x=1" if polls["n"] == 0 else "/pdp/lo/2?x=1"

    def count(_sel):
        polls["n"] += 1
        return 0 if polls["n"] <= 2 else 5

    ok &= check("a real turn -> ADVANCED",
                page_flow.advance_to_next_page(
                    lambda: True, href, count, lambda ms: None,
                    timeout_ms=20_000) == page_flow.ADVANCED)

    # The tracking tail must not make an unchanged card look changed: a
    # card's href carries a per-load pwa_ts and searchId.
    same = ["/pdp/lo/1?pwa_ts=1", "/pdp/lo/1?pwa_ts=2"]
    idx = {"i": 0}

    def href_same():
        value = same[min(idx["i"], 1)]
        idx["i"] += 1
        return value

    ok &= check("the same card with a new timestamp is NOT a turn",
                page_flow.advance_to_next_page(
                    lambda: True, href_same, lambda s: 5, lambda ms: None,
                    timeout_ms=3_000) == page_flow.NO_TURNOVER)

    group("The stop reasons that must NOT read as complete")
    ok &= check("next_page_throttled is not complete",
                "next_page_throttled" not in COMPLETE_STOP_REASONS)
    ok &= check("cards_missing is not complete",
                "cards_missing" not in COMPLETE_STOP_REASONS)
    ok &= check("page_cap is not complete", "page_cap" not in COMPLETE_STOP_REASONS)
    ok &= check("pagination_exhausted IS complete (the listing ran out)",
                "pagination_exhausted" in COMPLETE_STOP_REASONS)
    ok &= check("no_new_products IS complete",
                "no_new_products" in COMPLETE_STOP_REASONS)
    # And the engines must actually use those names.
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        ok &= check(f"{engine} reports next_page_throttled",
                    '"next_page_throttled"' in src)
        ok &= check(f"{engine} downgrades a card gap to cards_missing",
                    '"cards_missing"' in src)
    return ok


def test_output_contract():
    group("The output contract")
    ok = True
    names = [f.name for f in fields(Product)]
    # The family prefix, in the family's order. Four columns are deliberately
    # absent and output_writer's docstring carries the measurement for each.
    ok &= check("the family prefix leads, in order",
                names[:14] == ["source", "scraped_at", "url", "sku", "title",
                               "price", "currency", "rating", "review_count",
                               "image_url", "category", "price_source",
                               "page", "position"])
    for absent in ("original_price", "discount_pct", "brand", "in_stock"):
        ok &= check(f"{absent} is absent, with the measurement written down",
                    absent not in names)
    doc = Product.__module__ and sys.modules["output_writer"].__doc__ or ""
    ok &= check("output_writer's docstring says why each is absent",
                all(x in doc for x in ("original_price", "discount_pct",
                                       "brand", "in_stock")))
    ok &= check("site-specific columns come after the prefix",
                names.index("rating_scale") > names.index("position"))
    ok &= check("both modes map to a row class",
                set(ROW_CLASS_BY_MODE) == {"listing", "property"})
    ok &= check("both modes are one row per sku",
                set(UNIQUE_BY_SKU_MODES) == {"listing", "property"})

    group("Exit codes")
    ok &= check("0 ok / 3 blocked / 4 empty / 5 remote / 6 partial",
                (EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_API_ERROR, EXIT_PARTIAL)
                == (3, 4, 5, 6))
    return ok


def test_writers_and_finish_run():
    group("Writers")
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        # A run that finds nothing writes NOTHING — never replacing last
        # night's good output with [].
        with open(prefix + ".json", "w") as f:
            f.write('[{"sku": "keep-me"}]')
        rc = save([], prefix, "both")
        ok &= check("0 rows returns exit 4", rc == EXIT_NO_PRODUCTS)
        ok &= check("0 rows leaves the previous good output alone",
                    "keep-me" in open(prefix + ".json").read())
        rc = save([], prefix, "json", allow_empty=True)
        ok &= check("--allow-empty is the opt-out", rc == EXIT_NO_PRODUCTS
                    and json.load(open(prefix + ".json")) == [])

        # An empty CSV still carries its header, so a consumer reads a table
        # with no rows instead of failing on a zero-byte file.
        csv_path = os.path.join(tmp, "empty.csv")
        write_csv([], csv_path, row_cls=Product)
        header = next(csv_module.reader(open(csv_path)))
        ok &= check("an empty CSV keeps the header",
                    header == [f.name for f in fields(Product)])

        # A list column round-trips through a separator rather than repr().
        row = rows_of("LISTING_US")[1]
        csv2 = os.path.join(tmp, "rows.csv")
        write_csv([row], csv2, row_cls=Product)
        body = list(csv_module.DictReader(open(csv2)))[0]
        ok &= check("a list column is joined, not repr()'d",
                    body["badges"] == LIST_CSV_SEPARATOR.join(row.badges))

        group("finish_run — the status/exit mapping all three engines share")
        def run(rows, stop_reason, blocked=False, allow_empty=False):
            out = os.path.join(tmp, f"r{abs(hash(stop_reason))}{len(rows)}{blocked}")
            code = finish_run(rows, out, "json", allow_empty, blocked=blocked,
                              stop_reason=stop_reason, pages_requested=2,
                              pages_completed=1, start_url="u", final_url="u")
            meta_path = out + ".meta.json"
            meta = json.load(open(meta_path)) if os.path.exists(meta_path) else None
            return code, meta

        rows = rows_of("LISTING_US")
        code, meta = run(rows, "completed")
        ok &= check("a finished run is complete, exit 0",
                    code == 0 and meta["status"] == "complete")
        code, meta = run(rows, "next_page_throttled")
        ok &= check("a THROTTLED run is partial, exit 6",
                    code == EXIT_PARTIAL and meta["status"] == "partial")
        code, meta = run(rows, "cards_missing")
        ok &= check("a run with a card gap is partial, exit 6",
                    code == EXIT_PARTIAL and meta["status"] == "partial")
        code, meta = run(rows, "pagination_exhausted")
        ok &= check("a listing that ran out is complete, exit 0",
                    code == 0 and meta["status"] == "complete")
        code, meta = run([], "blocked_bot-or-not", blocked=True)
        ok &= check("blocked with no rows is exit 3", code == EXIT_BLOCKED)
        ok &= check("a FAILED run writes no sidecar beside good data",
                    meta is None)
        code, meta = run([], "completed")
        ok &= check("empty and not blocked is exit 4", code == EXIT_NO_PRODUCTS)

        group("The sidecar records WHICH pages failed, by number")
        out = os.path.join(tmp, "meta")
        finish_run(rows, out, "json", False, blocked=False,
                   stop_reason="blocked_x", pages_requested=5, pages_completed=3,
                   pages_failed=[2, 4], start_url="u", final_url="u",
                   mode="listing", source="vrbo.com",
                   extra={"cards_missing": {2: 10}, "results_total": 300})
        meta = json.load(open(out + ".meta.json"))
        ok &= check("pages_failed is a list of numbers", meta["pages_failed"] == [2, 4])
        ok &= check("mode and source are recorded",
                    meta["mode"] == "listing" and meta["source"] == "vrbo.com")
        ok &= check("the card gap rides in the sidecar",
                    meta["cards_missing"] == {"2": 10})
        ok &= check("extra cannot overwrite a run field",
                    meta["status"] == "partial")

    group("Merging and dedupe")
    seen = set()
    p1 = rows_of("LISTING_US", 1)
    ok &= check("a fresh page keeps every row",
                len(dedupe_by_key(p1, seen)) == len(p1))
    ok &= check("the same page again is fully dropped",
                dedupe_by_key(rows_of("LISTING_US", 2), seen) == [])
    ok &= check("dedupe_by_sku is the same function",
                dedupe_by_sku([], set()) == [])
    # A row with no key is always kept: there is nothing to check a duplicate
    # against, and dropping it would be a silent data loss.
    keyless = [Product(sku=None, title="a"), Product(sku=None, title="b")]
    ok &= check("keyless rows are kept, not collapsed",
                len(dedupe_by_key(keyless, set())) == 2)
    return ok


def test_diff():
    group("diff_runs — what counts as a price change here")
    ok = True

    def row(**kw):
        base = dict(sku="1", title="A place", price=100.0, currency="USD",
                    price_source="card", stay_dates="Sep 1 - Sep 2")
        base.update(kw)
        return [base]

    out = diff_products(row(), row(price=140.0))
    ok &= check("a real price change, same stay and same node, is `changed`",
                len(out["changed"]) == 1 and not out["stay_changed"])

    # THE bucket that matters on this site. A dateless search quotes every
    # property its own cheapest night, so the night moves between runs and
    # the amount moves with it. Reporting that as a repricing would make
    # every overnight diff look like the catalogue changed its mind.
    out = diff_products(row(), row(price=140.0, stay_dates="Oct 4 - Oct 5"))
    ok &= check("a price move with a MOVED STAY is not a price change",
                len(out["stay_changed"]) == 1 and not out["changed"])
    ok &= check("and the bucket says which nights",
                out["stay_changed"][0]["stay_dates"]["new"] == "Oct 4 - Oct 5")

    # A price difference that comes with a price_source difference says
    # something about our own two snapshots, not about the site.
    out = diff_products(row(), row(price=140.0, price_source="card-a11y"))
    ok &= check("a price_source change is NOT a price change",
                len(out["source_changed"]) == 1 and not out["changed"])

    ok &= check("a non-price column still compares normally",
                len(diff_products(row(), row(review_count=5,
                                             rating=9.0))["changed"]) == 1)
    ok &= check("the family's lifecycle key is still emitted, empty",
                diff_products(row(), row())["lifecycle"] == [])
    ok &= check("--fail-on-change ignores stay_changed",
                "stay_changed" not in _fail_on_change_source())

    group("diff_runs refuses what it cannot compare")
    # File-based, because that is how the tool is actually invoked.
    import argparse
    import diff_runs
    with tempfile.TemporaryDirectory() as tmp:
        def write(name, rows, status="complete", **meta_kw):
            prefix = os.path.join(tmp, name)
            with open(prefix + ".json", "w", encoding="utf-8") as f:
                json.dump(rows, f)
            meta = run_meta(status=status, stop_reason="completed",
                            pages_requested=1, pages_completed=1,
                            start_url="u", final_url="u",
                            products=len(rows), **meta_kw)
            with open(prefix + ".meta.json", "w", encoding="utf-8") as f:
                json.dump(meta, f)
            return prefix + ".json"

        def comparable(old, new):
            args = argparse.Namespace(old=old, new=new, force=False)
            with contextlib.redirect_stdout(io.StringIO()):
                return diff_runs._check_comparable(args)

        good = write("good", row(), mode="listing", source="vrbo.com")
        good2 = write("good2", row(price=110.0), mode="listing", source="vrbo.com")
        ok &= check("two complete listing runs compare", comparable(good, good2))
        partial = write("partial", row(), status="partial", mode="listing",
                        source="vrbo.com")
        ok &= check("a partial run is refused", not comparable(good, partial))
        other_mode = write("mode", row(), mode="property", source="vrbo.com")
        ok &= check("two different modes are refused",
                    not comparable(good, other_mode))
        # Five storefronts, five catalogues, and NOT one currency between
        # them — diffing two would report every row as added and removed.
        de = write("de", row(currency="EUR"), mode="listing",
                   source="fewo-direkt.de")
        ok &= check("two different storefronts are refused",
                    not comparable(good, de))
    return ok


def _fail_on_change_source():
    """The `--fail-on-change` condition, as written."""
    src = open(os.path.join(REPO_ROOT, "diff_runs.py"), encoding="utf-8").read()
    match = re.search(r"if args\.fail_on_change and \(([^)]*)\)", src)
    return match.group(1) if match else src


def test_env_config():
    group("env_config — precedence and placeholders")
    ok = True
    ok &= check("every ENV_KEYS value is a real CLI destination",
                set(env_config.ENV_KEYS.values()) ==
                {"twocaptcha_key", "cdp_endpoint", "proxy", "url",
                 "load_media", "proxy_budget_mb"})
    # A variable mapped onto a flag with a non-empty default would be
    # silently inert — a setting that looks configurable and is not.
    ok &= check("--out is deliberately NOT mapped",
                "out" not in env_config.ENV_KEYS.values())

    # .env.example must document exactly what the code reads, both ways.
    example = open(os.path.join(REPO_ROOT, ".env.example"), encoding="utf-8").read()
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example, re.M))
    ok &= check("every ENV_KEYS name is in .env.example",
                set(env_config.ENV_KEYS) <= documented)
    ok &= check("every .env.example name is read by the code",
                documented <= set(env_config.ENV_KEYS))

    group("A COPIED .env.example must read as unset")
    # `cp .env.example .env` followed by a run used to connect with the
    # literal string `{login}-zone-...` as a username and get a 401 — the
    # confusing auth error a long way from its cause that this rule exists to
    # prevent. Round-tripped through the real loader.
    saved = {k: os.environ.get(k) for k in env_config.ENV_KEYS}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write(example)
            with contextlib.redirect_stderr(io.StringIO()):
                env_config.load_env(path, override=True)
                # Every CREDENTIAL in the example must read as unset. The
                # two credentialled URLs are written the way the vendor
                # documents them, so a literal-only placeholder list misses
                # both — `cp .env.example .env` then connected with the
                # string `{login}-zone-...` as a username and got a 401 a
                # long way from its cause.
                for name in ("TWOCAPTCHA_KEY", "VRBO_CDP_ENDPOINT",
                             "VRBO_PROXY"):
                    ok &= check(f"{name} from a copied example reads as unset",
                                env_config.env_value(name) is None)
                # And the non-credential default must still be USABLE, or
                # the check above would pass by making everything unset.
                url = env_config.env_value("VRBO_URL")
            ok &= check("VRBO_URL from the example survives and is usable",
                        url is not None and is_supported_host(url))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return ok


def test_proxy_pool():
    group("Credentials never reach argv or a log")
    ok = True
    url = "http://user:" + "s3cr3t" + "@exit.example.com:2334"
    masked = mask(url)
    ok &= check("the password is masked", "s3cr3t" not in masked)
    ok &= check("the host and port are KEPT — that is the point of the log",
                "exit.example.com" in masked and "2334" in masked)
    scrubbed, credentials = split_credentials(url)
    ok &= check("split_credentials strips them from the address",
                "s3cr3t" not in scrubbed and credentials == ("user", "s3cr3t"))
    pw = to_playwright(url)
    ok &= check("Playwright gets them in its own fields, not in the server URL",
                pw["password"] == "s3cr3t" and "s3cr3t" not in pw["server"])

    group("A worker owns one exit; rotation is a fresh browser")
    pool = ProxyPool(["http://a@h1:1", "http://b@h2:2", "http://c@h3:3"])
    first = pool.current
    pool.advance("test")
    ok &= check("advance moves to a different exit", pool.current != first)
    ok &= check("the pool knows its size", len(pool) == 3)
    return ok


def test_credentials_never_reach_a_log():
    group("An EXCEPTION MESSAGE is a log")
    ok = True
    secret = "hunter2"
    # Concatenated rather than interpolated, so no line in this file holds a
    # complete `scheme://user:pass@host` literal. That keeps ci_checks.py's
    # credential scan meaningful on the one file where a real credential is
    # most likely to be pasted while debugging — an allowlist entry here
    # would switch the check off exactly where it matters.
    endpoint = "ws://user:" + secret + "@cb.2captcha.com:9222"
    for engine in ENGINES:
        try:
            module = __import__(engine)
        except ImportError:
            continue
        masker = getattr(module, "_mask_credentials", None)
        if masker is None:
            ok &= check(f"{engine} has a credential masker", False)
            continue
        # Globally, not once: a Playwright connection error repeats the
        # endpoint five times, and a masker that handles the first prints the
        # password the other four while looking like it works.
        repeated = " ".join([endpoint] * 5)
        ok &= check(f"{engine} masks EVERY occurrence",
                    secret not in masker(repeated))
        ok &= check(f"{engine} keeps the host and port",
                    "cb.2captcha.com:9222" in masker(endpoint))
    # And the solver redacts a key out of an error message, because the
    # fingerprint API takes its key as a query parameter and `requests` puts
    # the full URL into the text of every error it raises.
    key = "a" * 32
    redacted = captcha_solver._redact(f"GET https://x/y?key={key} failed")
    ok &= check("the solver redacts a key from an error message",
                key not in redacted)
    return ok


def test_engine_parity(skips):
    group("The three engines agree — flags, in BOTH directions")
    ok = True
    flagsets = {}
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        flagsets[engine] = set(re.findall(r'p\.add_argument\("(--[a-z0-9-]+)"', src))

    # The family contract. Every engine must carry all of these.
    contract = {"--url", "--pages", "--category", "--format", "--out", "--delay",
                "--retries", "--retry-delay", "--concurrency", "--proxy",
                "--proxy-file", "--proxy-rotate", "--proxy-shuffle",
                "--proxy-block-retries", "--twocaptcha-key", "--captcha-api",
                "--solve-captcha", "--min-score", "--cdp-endpoint",
                "--allow-empty", "--dump-html", "--headless", "--headful",
                "--mode"}
    for engine, flags in flagsets.items():
        missing = contract - flags
        ok &= check(f"{engine} carries the whole contract "
                    f"{'' if not missing else sorted(missing)}", not missing)

    # And the DOCUMENTED differences, asserted in both directions so closing
    # one needs a README edit rather than a quiet patch.
    documented_extra = {
        # --cdp-connect-timeout is on the two engines that can actually USE
        # an authenticated CDP endpoint. Selenium cannot (chromedriver's
        # debuggerAddress has nowhere to put a password), so a connect
        # timeout there would be a flag for a path that does not exist.
        "playwright_scraper": {"--locale", "--fingerprint", "--fp-tags",
                               "--fp-country", "--browser-channel",
                               "--cdp-connect-timeout",
                               "--block-retry-delay"},
        "selenium_scraper": {"--locale", "--fingerprint", "--fp-tags",
                             "--fp-country", "--block-retry-delay"},
        "puppeteer_scraper": {"--chromium-path", "--cdp-connect-timeout",
                              "--block-retry-delay"},
    }
    for engine, extra in documented_extra.items():
        actual = flagsets[engine] - contract
        ok &= check(f"{engine}'s extra flags are exactly the documented set",
                    actual == extra)

    group("Every shared-module call binds against the real signature")
    problems = []
    for engine in ENGINES:
        path = os.path.join(REPO_ROOT, f"{engine}.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in SHARED_MODULES:
                for alias in node.names:
                    imported[alias.asname or alias.name] = (node.module, alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = None
            if isinstance(node.func, ast.Name) and node.func.id in imported:
                module, name = imported[node.func.id]
                fn = getattr(SHARED_MODULES[module], name, None)
            elif (isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id in SHARED_MODULES):
                fn = getattr(SHARED_MODULES[node.func.value.id], node.func.attr, None)
            if fn is None or not callable(fn):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue
            if any(k.arg is None for k in node.keywords):
                continue
            try:
                signature = inspect.signature(fn)
            except (TypeError, ValueError):
                continue
            try:
                signature.bind(*[object()] * len(node.args),
                               **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                problems.append(f"{engine}:{node.lineno} {getattr(fn,'__name__','?')}: {exc}")
    ok &= check(f"no call site disagrees with its callee "
                f"{'' if not problems else problems[:3]}", not problems)

    group("Every engine imports its driver at MODULE level")
    # Without this the module imports cleanly with no driver installed, the
    # skip below never fires, and CI's engine-smoke job cannot notice a
    # broken import.
    drivers = {"playwright_scraper": "playwright",
               "selenium_scraper": "selenium",
               "puppeteer_scraper": "pyppeteer"}
    for engine, driver in drivers.items():
        tree = ast.parse(open(os.path.join(REPO_ROOT, f"{engine}.py"),
                              encoding="utf-8").read())
        top_level = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(driver):
                top_level.append(node)
            if isinstance(node, ast.Import):
                top_level += [a for a in node.names if a.name.startswith(driver)]
        ok &= check(f"{engine} imports {driver} at module level", bool(top_level))

    group("The browser channel is the same string in every engine")
    # A bundled Chromium is refused here, so an engine that quietly differed
    # on this would be an engine that cannot fetch the site.
    try:
        import playwright_scraper as pws
        ok &= check("Playwright defaults to real Chrome",
                    pws.DEFAULT_BROWSER_CHANNEL == "chrome")
    except ImportError:
        skips.append("playwright_scraper (playwright not installed)")
    sel = open(os.path.join(REPO_ROOT, "selenium_scraper.py"), encoding="utf-8").read()
    ok &= check("Selenium says it drives the installed Chrome",
                "real Chrome for free" in sel)
    pup = open(os.path.join(REPO_ROOT, "puppeteer_scraper.py"), encoding="utf-8").read()
    ok &= check("pyppeteer warns BEFORE the run when --chromium-path is unset",
                "No --chromium-path" in pup)

    group("The price floor is one number, not three")
    floors = []
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        match = re.search(r"^PRICE_FLOOR = (\d+)", src, re.M)
        floors.append(match.group(1) if match else None)
    ok &= check(f"all three engines share a PRICE_FLOOR ({floors[0]})",
                len(set(floors)) == 1 and floors[0] is not None)

    group("Engines import cleanly (skipped if the driver is absent)")
    for engine in ENGINES:
        try:
            __import__(engine)
            ok &= check(f"{engine} imports", True)
        except ImportError as exc:
            skips.append(f"{engine} ({exc})")
            print(f"  SKIP  {engine} — {exc}")
    return ok


def test_no_undefined_names():
    group("Names that resolve, not just parse")
    # `compileall` proves a file PARSES, not that its names RESOLVE. A live
    # run of a sibling repo's engine died with NameError on a line reached
    # only while fetching, after an import had been removed — invisible to
    # import, --help, compileall and 400+ green assertions. Kept COARSE so it
    # under-reports rather than inventing problems.
    ok = True
    for name in sorted(os.listdir(REPO_ROOT)):
        if not name.endswith(".py") or name == "smoke_test.py":
            continue
        undefined = _undefined_names(os.path.join(REPO_ROOT, name))
        ok &= check(f"{name}: no undefined names "
                    f"{'' if not undefined else sorted(undefined)[:5]}", not undefined)
    return ok


def _undefined_names(path):
    source = open(path, encoding="utf-8").read()
    tree = ast.parse(source)
    bound = set(dir(__builtins__) if not isinstance(__builtins__, dict)
                else __builtins__.keys())
    bound |= set(dir(__import__("builtins")))
    # Module-level dunders are always bound and are not imports.
    bound |= {"__file__", "__name__", "__doc__", "__package__", "__spec__",
              "__loader__", "__builtins__", "__debug__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                for arg in (args.posonlyargs + args.args + args.kwonlyargs):
                    bound.add(arg.arg)
                if args.vararg:
                    bound.add(args.vararg.arg)
                if args.kwarg:
                    bound.add(args.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            pass
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return used - bound


def test_dockerfile_matches_its_entrypoint():
    group("The Dockerfile COPY list against the import graph")
    # All three repos in this family once shipped an image that died with
    # ModuleNotFoundError on every invocation, --help included, because one
    # module was missing from an explicit COPY list. This check needs no
    # Docker.
    ok = True
    path = os.path.join(REPO_ROOT, "Dockerfile")
    if not os.path.exists(path):
        return check("a Dockerfile exists", False)
    dockerfile = open(path, encoding="utf-8").read()
    # Join backslash continuations first: the COPY list spans five lines, and
    # a line-by-line reader sees an empty list and passes vacuously.
    joined = re.sub(r"\\\s*\n\s*", " ", dockerfile)
    copied = set()
    for line in joined.splitlines():
        if line.strip().upper().startswith("COPY"):
            # [1:-1]: the first token is COPY and the LAST is the
            # destination. Including the destination made `./` look like
            # "copy everything" and the check passed vacuously.
            for token in line.split()[1:-1]:
                if token.endswith(".py"):
                    copied.add(os.path.basename(token))
                elif token in ("./", "."):
                    copied.update(n for n in os.listdir(REPO_ROOT)
                                  if n.endswith(".py"))

    # Walk the entrypoint's own import graph.
    entrypoints = [n for n in ENGINES if f"{n}.py" in dockerfile]
    if not entrypoints:
        entrypoints = ["playwright_scraper"]
    needed, queue = set(), list(entrypoints)
    local = {n[:-3] for n in os.listdir(REPO_ROOT) if n.endswith(".py")}
    while queue:
        module = queue.pop()
        if module in needed:
            continue
        needed.add(module)
        tree = ast.parse(open(os.path.join(REPO_ROOT, f"{module}.py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in local:
                queue.append(node.module)
            elif isinstance(node, ast.Import):
                queue += [a.name for a in node.names if a.name in local]
    missing = {f"{m}.py" for m in needed} - copied
    ok &= check(f"every module the entrypoint imports is COPYed "
                f"{'' if not missing else sorted(missing)}", not missing)

    group("The image carries no secrets and no test material")
    for unwanted in (".env", "smoke_test.py", "fixtures_generated.json",
                     "captures"):
        ok &= check(f"{unwanted} is not COPYed into the image",
                    unwanted not in copied and f"COPY {unwanted}" not in dockerfile)
    return ok


def test_wording():
    group("Wording enforced by a test")
    ok = True
    banned = {
        "cloud browser": "Scraping Browser API",
        "antidetect browser": "Scraping Browser API",
        "gate.2prx.com": "2captcha.com/proxy",
        "2prx.com": "2captcha.com/proxy",
        "--antidetect": "removed",
        "ANTIDETECT_LOCAL_API": "removed",
    }
    shipped = [n for n in os.listdir(REPO_ROOT)
               if n.endswith((".py", ".md", ".txt", ".toml", ".yml", ".example"))]
    for name in shipped:
        if name == "smoke_test.py":
            continue  # this file names them in order to ban them
        text = open(os.path.join(REPO_ROOT, name), encoding="utf-8",
                    errors="replace").read().lower()
        for phrase, instead in banned.items():
            ok &= check(f"{name}: no {phrase!r} (write {instead!r})",
                        phrase.lower() not in text)

    group("Removed flags stay removed — scoped to the ENGINES")
    # --country is banned on a scraper (it could disagree with the URL, and
    # here the storefront IS the hostname) and legitimate on
    # fingerprint_client.py, where it picks a fingerprint locale.
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        ok &= check(f"{engine} has no --country flag",
                    'add_argument("--country"' not in src)
    return ok


def test_no_capture_leaks():
    group("Committed fixtures carry no credential-shaped material")
    ok = True
    text = open(_FIXTURE_PATH, encoding="utf-8").read()
    # PATTERNS, not the literals one capture happened to contain, so the next
    # capture is caught too.
    patterns = {
        "a 24+ char hex run": r"\b[0-9a-fA-F]{24,}\b",
        "a reCAPTCHA site key": r"\b6L[A-Za-z0-9_-]{20,}",
        "a Turnstile site key": r"\b0x4[A-Za-z0-9]{15,}",
        "an embedded credential": r"[a-z]+://[^\s\"/@]+:[^\s\"/@]+@",
    }
    for label, pattern in patterns.items():
        found = re.findall(pattern, text)
        ok &= check(f"no {label} in fixtures_generated.json "
                    f"{'' if not found else found[:2]}", not found)
    ok &= check("the challenge fixture still names its vendor after scrubbing",
                challenge_vendor(fixture("BLOCK_429")) == "datadome-challenge")
    return ok


def test_ci_checks_is_wired_up():
    group("One credential check, invoked from CI and from here")
    ok = True
    script = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check("ci_checks.py exists", os.path.exists(script))
    workflow = os.path.join(REPO_ROOT, ".github", "workflows", "tests.yml")
    if os.path.exists(workflow):
        text = open(workflow, encoding="utf-8").read()
        # A check nothing runs is not a check; two sources of truth that
        # disagree is worse.
        ok &= check("tests.yml CALLS ci_checks.py rather than reimplementing it",
                    "ci_checks.py" in text)
    if os.path.exists(script):
        # And it must pass on THIS repo. A check that fails on its own
        # repository is a check nobody can read.
        import subprocess
        result = subprocess.run([sys.executable, script, "--all"],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        ok &= check(f"ci_checks.py passes on this repo "
                    f"{'' if result.returncode == 0 else result.stdout[-300:]}",
                    result.returncode == 0)
        ok &= _scan_is_relative_to_the_repo(script)
    return ok


def _scan_is_relative_to_the_repo(script):
    """The scan must not depend on where the clone lives.

    It used to test the ABSOLUTE path's parts against `venv`/`.venv`, so a
    clone under any directory with one of those names skipped every file,
    printed "0 files scanned" and passed with a key planted in the tree.
    """
    import shutil
    import subprocess
    ok = True
    # Built at run time so no line of this file is key-shaped itself.
    planted = "".join("ab" for _ in range(16))
    with tempfile.TemporaryDirectory() as tmp:
        clone = os.path.join(tmp, "venv", "vrbo-scraper")
        os.makedirs(os.path.join(clone, ".github"))
        shutil.copy(script, os.path.join(clone, ".github", "ci_checks.py"))
        copied = os.path.join(clone, ".github", "ci_checks.py")

        def scan():
            return subprocess.run([sys.executable, copied, "--secret-check"],
                                  cwd=clone, capture_output=True, text=True)

        # A real virtualenv INSIDE the clone is still skipped, by its
        # pyvenv.cfg rather than by its name.
        env = os.path.join(clone, "myenv")
        os.makedirs(os.path.join(env, "lib"))
        open(os.path.join(env, "pyvenv.cfg"), "w").close()
        with open(os.path.join(env, "lib", "vendored.py"), "w") as f:
            f.write(f"X = '{planted}'\n")
        result = scan()
        ok &= check("a clone under a parent named venv scans its files, and a "
                    "nested real venv is skipped",
                    result.returncode == 0 and " 0 files" not in result.stdout)
        with open(os.path.join(clone, "note.txt"), "w") as f:
            f.write(f"TWOCAPTCHA_KEY={planted}\n")
        result = scan()
        ok &= check("...and a key planted there FAILS the scan",
                    result.returncode != 0 and "note.txt" in result.stdout)
        os.remove(os.path.join(clone, "note.txt"))
        with open(os.path.join(clone, "fixture.json"), "w") as f:
            f.write(json.dumps({"html": f"key={planted}"}))
        result = scan()
        ok &= check("a key inside a JSON fixture FAILS the scan",
                    result.returncode != 0 and "fixture.json" in result.stdout)

    import importlib.util
    spec = importlib.util.spec_from_file_location("_ci_checks_copy", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.scanned_files = lambda: iter(())
    with contextlib.redirect_stdout(io.StringIO()):
        failures = module.secret_check()
    ok &= check("a scan that read 0 files is a failure, not a pass",
                bool(failures))
    return ok


def test_sample_output():
    group("sample_output is cut from a real run")
    ok = True
    for name, loader in (("sample_output.json", json.load),):
        path = os.path.join(REPO_ROOT, name)
        if not os.path.exists(path):
            ok &= check(f"{name} exists", False)
            continue
        rows = loader(open(path, encoding="utf-8"))
        ok &= check(f"{name} is a non-empty list", isinstance(rows, list) and rows)
        columns = [f.name for f in fields(Product)]
        ok &= check(f"{name} columns match Product exactly",
                    all(list(r) == columns for r in rows))
        # Fabrication markers — a sample nobody ran reads exactly like one
        # somebody did.
        text = json.dumps(rows)
        for marker in ("example.com", "lorem", "PLACEHOLDER", "TODO", "foo bar"):
            ok &= check(f"{name}: no {marker!r}", marker.lower() not in text.lower())
        ok &= check(f"{name}: every row is from a supported storefront",
                    all(r["source"] in HOSTS for r in rows))
        ok &= check(f"{name}: page+position unique",
                    len({(r["page"], r["position"]) for r in rows}) == len(rows))
    csv_path = os.path.join(REPO_ROOT, "sample_output.csv")
    if os.path.exists(csv_path):
        header = next(csv_module.reader(open(csv_path, encoding="utf-8")))
        ok &= check("sample_output.csv header matches Product",
                    header == [f.name for f in fields(Product)])
    else:
        ok &= check("sample_output.csv exists", False)
    return ok


def test_required_files_are_committed():
    group("Everything the suite needs is tracked by git")
    # A blanket `*.json` / `*.csv` in .gitignore — which this repo wants,
    # because a scraper's own output is large and stale — silently swallowed
    # `fixtures_generated.json`. The suite was green on the machine that
    # wrote it and every CI job died with FileNotFoundError at import. A
    # check that a file EXISTS cannot see that; only asking git can.
    ok = True
    import subprocess
    required = ("fixtures_generated.json", "sample_output.json",
                "sample_output.csv", ".env.example", "README.md",
                "CHANGELOG.md", "Dockerfile", "requirements.txt",
                ".github/ci_checks.py", ".github/workflows/tests.yml",
                ".github/workflows/canary.yml", ".github/canary_check.py",
                "tests/test_smoke.py")
    result = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                            capture_output=True, text=True)
    if result.returncode != 0:
        print("  SKIP  not a git checkout — cannot verify what is committed")
        return ok
    tracked = set(result.stdout.split())
    for name in required:
        ok &= check(f"{name} is committed, not just present on disk",
                    name in tracked)
    # And the other direction: nothing a scraper produced should be.
    leaked = [f for f in tracked
              if re.search(r"_debug\.(html|png)$|\.meta\.json$|^captures/|^\.env$",
                           f)]
    ok &= check(f"no run output or capture is committed "
                f"{'' if not leaked else leaked[:3]}", not leaked)
    return ok


def test_readme_claims():
    group("README numbers exist and are dated")
    ok = True
    path = os.path.join(REPO_ROOT, "README.md")
    if not os.path.exists(path):
        return check("README.md exists", False)
    readme = open(path, encoding="utf-8").read()
    ok &= check("the README names every supported storefront",
                all(h in readme for h in HOSTS))
    ok &= check("it states the real-Chrome finding, which is the whole game",
                "429" in readme and "chrome" in readme.lower())
    ok &= check("it warns about dateless pricing",
                "startDate" in readme)
    ok &= check("it says concurrency is refused",
                "--concurrency" in readme)
    # Every claim is measured or absent: a number without a date goes stale
    # invisibly.
    ok &= check("measurements carry a date", "2026-09-14" in readme)
    return ok


# The floor the README and CHANGELOG state. A FLOOR rather than the exact
# count, because an exact count goes stale the next time anyone adds a check
# and a stale number in a README is worse than no number. Raise it when
# it is comfortably passed; it can only ever be an under-claim.
CLAIMED_CHECK_FLOOR = 450


def test_x_debug_header_is_redacted():
    """SECURITY.md names the Scraper API's x-debug header as a place
    credentials reach a log unmasked. It was then logged verbatim.

    The fixtures are assembled from pieces rather than written out whole,
    because this file is scanned by the credential check like every other
    and a fixture that LOOKS like a live key fails it. They are the SHAPES a
    credential takes, not the literals this repo happens to contain today.
    """
    try:
        import scraper_api_client as sac
    except ImportError:
        return False

    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.00145 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    ok = True
    ok &= check("x-debug: the credential and the key are gone",
                      pw not in out and key not in out)
    ok &= check("x-debug: the cost, host and status survive",
                      "cost=0.00145" in out and "cb.2captcha.com:9222" in out
                      and "status=200" in out)

    s1, s2 = "secret" + "one", "secret" + "two"
    two = sac._redact_debug_header(
        "a=http://u1:" + s1 + "@h1:1 b=http://u2:" + s2 + "@h2:2")
    ok &= check("x-debug: both credentials are masked, not just the first",
                      s1 not in two and s2 not in two)

    src = inspect.getsource(sac)
    ok &= check("x-debug: the log line calls the redactor",
                      'logger.info("x-debug: %s", _redact_debug_header(debug))' in src)
    return ok


def test_scraper_api_waitfor_is_an_object():
    """Both Scraper API defects measured 2026-09-23, through the real
    parse_args() and fetch_html(), with requests.post captured (no network).

    waitFor went out as a JSON-encoded STRING, which the live API refuses
    with HTTP 422 and still bills; and the target's status was read from
    `status`, which is the API's own verdict string ("success"), so a
    target 403/503 never reached detect_page_state. The real field is
    `http_code`."""
    group("Scraper API: waitFor is an object, the target status is http_code")
    try:
        import scraper_api_client as sac
    except ImportError as e:
        return check("scraper_api_client imports (%s)" % e, False)

    captured = {}

    class _Resp:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"status": "success", "http_code": 403, "headers": {},
                    "body": "<html></html>"}

    def _post(url, **kw):
        captured.update(kw)
        return _Resp()

    real_post, real_argv = sac.requests.post, sys.argv
    sac.requests.post = _post
    sys.argv = ["scraper_api_client.py", "--key", "k" * 8,
                "--url", "https://www.vrbo.com/search?destination=Orlando,%20Florida,%20United%20States%20of%20America", "--wait-text", "Orlando"]
    status = None
    try:
        args = sac.parse_args()
        got = sac.fetch_html(args)
        status = got[1] if isinstance(got, tuple) else None
    finally:
        sac.requests.post, sys.argv = real_post, real_argv
    wf = (captured.get("json") or {}).get("waitFor")
    ok = check("Scraper API: --wait-text sends waitFor as an OBJECT, not a "
               "JSON-encoded string (HTTP 422 and still billed, 2026-09-23)",
               isinstance(wf, dict) and wf.get("text") == "Orlando")
    ok &= check("Scraper API: the status handed onward is the target's "
                "http_code (403, an int), not the API's verdict 'success'",
                isinstance(status, int) and status == 403)
    return ok


# ---------------------------------------------------------------------------
# The engines' fetch loop, driven through a FAKE driver
# ---------------------------------------------------------------------------
# The three `_fetch_one_page` copies decide what a page cost and what a run
# reports, and the engine groups above skip whenever a driver library is
# absent. These checks load a private copy of each engine with stand-in
# driver modules where the real one is missing, replace every browser
# primitive with a stub, and run the REAL loop. No network, no solver call:
# `solve_recaptcha` is replaced by a counter.

_DRIVER_STUBS = {
    "playwright_scraper": {
        "playwright": {},
        "playwright.sync_api": {"sync_playwright": None,
                                "Error": type("Error", (Exception,), {}),
                                "TimeoutError": type("TimeoutError", (Exception,), {})},
    },
    "puppeteer_scraper": {
        "pyppeteer": {"launch": None, "connect": None},
    },
    "selenium_scraper": {
        "selenium": {},
        "selenium.webdriver": {},
        "selenium.common": {},
        "selenium.common.exceptions": {
            "TimeoutException": type("TimeoutException", (Exception,), {}),
            "WebDriverException": type("WebDriverException", (Exception,), {})},
        "selenium.webdriver.chrome": {},
        "selenium.webdriver.chrome.options": {"Options": object},
        "selenium.webdriver.common": {},
        "selenium.webdriver.common.by": {
            "By": type("By", (), {"CSS_SELECTOR": "css selector"})},
    },
}


def _load_engine_copy(engine):
    """A private copy of `engine`, importable whether or not its driver is."""
    import importlib.util
    import types
    added = []
    for modname, attrs in _DRIVER_STUBS[engine].items():
        try:
            __import__(modname)
            continue
        except ImportError:
            pass
        module = types.ModuleType(modname)
        for k, v in attrs.items():
            setattr(module, k, v)
        sys.modules[modname] = module
        added.append(modname)
    if "selenium" in added:
        sys.modules["selenium"].webdriver = sys.modules["selenium.webdriver"]
    try:
        spec = importlib.util.spec_from_file_location(
            f"_fake_driver_{engine}", os.path.join(REPO_ROOT, f"{engine}.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for modname in added:
            sys.modules.pop(modname, None)


class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakePage:
    """Every method the three engines call on a page or a driver."""

    def __init__(self, status):
        self.status, self.url, self.navigations = status, "https://www.vrbo.com/search?destination=Orlando", 0

    def goto(self, url, *a, **k):
        self.navigations += 1
        return _FakeResponse(self.status)

    def get(self, url):                    # selenium driver.get
        self.navigations += 1

    def evaluate(self, *a, **k):
        return None

    def execute_script(self, *a, **k):
        return None

    def reload(self, *a, **k):
        return None

    def refresh(self):
        return None

    def wait_for_timeout(self, ms):
        return None

    def screenshot(self, *a, **k):
        return None

    def save_screenshot(self, *a, **k):
        return None


class _FakeSession:
    def __init__(self, status):
        self.page = self.driver = _FakePage(status)
        self.bridge = type("Bridge", (), {"run": staticmethod(lambda x: x)})()
        self.relaunches = 0

    def relaunch(self):
        self.relaunches += 1


class _FakePool:
    current = "http://exit-1.invalid:1"

    def __len__(self):
        return 3

    def advance(self, reason=""):
        pass

    def rotates_per_page(self):
        return False


def _run_fake_page(engine_mod, states, rows=(), status=200, pool=None,
                   tmp="."):
    """Run one page through the engine's real `_fetch_one_page`.

    `states` is the sequence `page_flow.classify` answers with (the last one
    repeats). Returns (outcome, solver purchases, statuses the classifier
    was given).
    """
    import types
    bought, statuses, answers = [], [], list(states)

    def fake_classify(html, status=None, url=""):
        statuses.append(status)
        return answers.pop(0) if len(answers) > 1 else answers[0]

    # A widget is "on the page" only in the scenarios about challenges.
    challenge = (types.SimpleNamespace(kind="recaptcha_v2", source="stub",
                                       sitekey="stub", action=None)
                 if "challenge" in states else None)
    patches = {
        "_count": lambda *a, **k: 0,
        "_sleep": lambda ms: None,
        "_content": lambda *a, **k: "<html></html>",
        "_content_when_settled": lambda *a, **k: "<html></html>",
        "_current_url": lambda *a, **k: "https://www.vrbo.com/search?destination=Orlando",
        "_scroll_the_grid": lambda *a, **k: {},
        "_parse_for_mode": lambda *a, **k: list(rows),
        "detect_recaptcha_v3": lambda *a, **k: challenge,
        "detect_recaptcha_in_page": lambda *a, **k: None,
        "reconcile_detections": lambda *a, **k: challenge,
        "solve_recaptcha": lambda *a, **k: bought.append(1) or "stub-token",
        "time": types.SimpleNamespace(sleep=lambda s: None),
    }
    saved = {k: getattr(engine_mod, k) for k in patches if hasattr(engine_mod, k)}
    saved_classify = page_flow.classify
    for k, v in patches.items():
        if k in saved:
            setattr(engine_mod, k, v)
    page_flow.classify = fake_classify
    args = types.SimpleNamespace(
        pages=1, retries=1, retry_delay=0, block_retry_delay=None,
        proxy_block_retries=2, mode="listing", out=os.path.join(tmp, "fake"),
        dump_html=None, solve_captcha="when-blocked",
        twocaptcha_key="stub-never-sent", captcha_api="v2", min_score=0.7,
        headless=False, delay=0, chromium_path="/stub", cdp_endpoint=None)
    try:
        session = _FakeSession(status)
        outcome = engine_mod._fetch_one_page(
            session, args, pool, 1, "https://www.vrbo.com/search?destination=Orlando")
    finally:
        for k, v in saved.items():
            setattr(engine_mod, k, v)
        page_flow.classify = saved_classify
    return outcome, len(bought), statuses


def test_engine_fetch_loop():
    group("The fetch loop through a fake driver: solves, exits, status")
    ok = True
    row = rows_of("LISTING_US")[0]
    with tempfile.TemporaryDirectory() as tmp:
        for engine in ENGINES:
            mod = _load_engine_copy(engine)
            short = engine.split("_")[0]

            # A page that stays a challenge through every retry: one
            # purchase per PAGE, never one per attempt, with or without a
            # pool — and the run is blocked, not empty.
            for label, pool in (("no pool", None), ("a pool of 3", _FakePool())):
                outcome, bought, _ = _run_fake_page(mod, ["challenge"], pool=pool, tmp=tmp)
                ok &= check(f"{short}: an unsolved challenge buys 1 solve with "
                            f"{label} (bought {bought})",
                            bought == page_flow.SOLVES_PER_PAGE)
                ok &= check(f"{short}: an unsolved challenge ends blocked, not "
                            f"empty ({label})",
                            outcome.blocked_by is not None and not outcome.ok
                            and outcome.products == [])
            reason = page_flow.failure_stop_reason(outcome)
            code = finish_run([], os.path.join(tmp, f"{short}-ch"), "json",
                              False, blocked=outcome.blocked_by is not None,
                              stop_reason=reason, pages_requested=1,
                              pages_completed=0, start_url="u", final_url="u")
            ok &= check(f"{short}: ...and the run exits 3 ({reason}, got {code})",
                        code == EXIT_BLOCKED)

            # Served, never painted, 0 rows: content not obtained.
            outcome, bought, _ = _run_fake_page(mod, ["shell"], tmp=tmp)
            ok &= check(f"{short}: a shell that never painted is not ok "
                        f"({outcome.unobtained})",
                        not outcome.ok
                        and outcome.unobtained == "served_without_results"
                        and bought == 0)
            reason = page_flow.failure_stop_reason(outcome)
            code = finish_run([], os.path.join(tmp, f"{short}-sh"), "json",
                              False, blocked=False, stop_reason=reason,
                              pages_requested=1, pages_completed=0,
                              start_url="u", final_url="u")
            ok &= check(f"{short}: ...and the run exits 5, not 4 (got {code})",
                        code == EXIT_API_ERROR)

            # The grid is there and the parser lost it: parser failure.
            outcome, _, _ = _run_fake_page(mod, ["content"], rows=(), tmp=tmp)
            ok &= check(f"{short}: cards on the page and 0 rows is "
                        f"parser_found_nothing ({outcome.unobtained})",
                        outcome.unobtained == "parser_found_nothing"
                        and not outcome.ok)
            outcome, _, _ = _run_fake_page(mod, ["content"], rows=(row,), tmp=tmp)
            ok &= check(f"{short}: cards and rows is ok",
                        outcome.ok and len(outcome.products) == 1)
            # The site's own no-results copy stays a genuine empty answer.
            outcome, _, _ = _run_fake_page(mod, ["empty"], rows=(row,), tmp=tmp)
            ok &= check(f"{short}: `empty` is ok, unparsed, and still exit 4's case",
                        outcome.ok and outcome.products == []
                        and outcome.unobtained is None)

            # The navigation's status reaches the classifier.
            _, _, statuses = _run_fake_page(mod, ["content"], rows=(row,),
                                            status=503, tmp=tmp)
            if engine == "selenium_scraper":
                ok &= check("selenium: WebDriver has no status, so None is passed",
                            statuses and statuses[0] is None)
            else:
                ok &= check(f"{short}: the goto() status reaches the classifier "
                            f"({statuses[:1]})", statuses and statuses[0] == 503)

    group("The site's own error copy is not an empty catalogue (real capture)")
    html, url = FIXTURES["SITE_ERROR"], URLS["SITE_ERROR"]
    state = detect_page_state(html, None, url)
    ok &= check(f"it reads as a served shell ({state})", state == "shell")
    ok &= check("it parses to 0 rows", parse_products(html, url) == [])
    ok &= check("0 rows from it is served_without_results",
                page_flow.unobtained_reason(state, 0) == "served_without_results")
    ok &= check("the no-results copy is still a genuine empty answer",
                page_flow.unobtained_reason(
                    detect_page_state(FIXTURES["EMPTY_EN"], 200, URLS["EMPTY_EN"]), 0)
                is None)
    ok &= check("a real grid with rows is obtained",
                page_flow.unobtained_reason("content", 4) is None)
    return ok


def test_numeric_flags_are_validated():
    group("Counts and waits refuse values that cannot be honoured")
    ok = True
    import argparse
    for flag, kind, minimum, bad, good in (
            ("--pages", int, 1, "0", "2"), ("--concurrency", int, 1, "0", "1"),
            ("--proxy-block-retries", int, 0, "-1", "0"),
            ("--delay", float, 0, "-1", "0"), ("--retry-delay", float, 0, "-0.5", "1.5"),
            ("--block-retry-delay", float, 0, "-30", "30")):
        parse = page_flow.at_least(minimum, kind)
        ok &= check(f"{flag}: {good} is accepted", parse(good) == kind(good))
        ok &= check(f"{flag}: {bad} is refused",
                    _raises(lambda: parse(bad)))
    ok &= check("a NaN delay is refused",
                _raises(lambda: page_flow.at_least(0, float)("nan")))
    wired = ('"--pages", type=page_flow.at_least(1)',
             '"--delay", type=page_flow.at_least(0, float)',
             '"--concurrency", type=page_flow.at_least(1)',
             '"--retry-delay", type=page_flow.at_least(0, float)',
             '"--block-retry-delay", type=page_flow.at_least(0, float)',
             '"--proxy-block-retries", type=page_flow.at_least(0)')
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        missing = [w.split(",")[0] for w in wired if w not in src]
        ok &= check(f"{engine} validates every count and wait {missing or ''}",
                    not missing)
    # And through a real argparse error, which is what a user sees: exit 2
    # with the flag named.
    p = argparse.ArgumentParser()
    p.add_argument("--delay", type=page_flow.at_least(0, float))
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            p.parse_args(["--delay", "-1"])
            code = 0
        except SystemExit as e:
            code = e.code
    ok &= check("argparse exits 2 naming the flag",
                code == 2 and "--delay" in err.getvalue())
    api = open(os.path.join(REPO_ROOT, "scraper_api_client.py"), encoding="utf-8").read()
    ok &= check("scraper_api_client validates --retries and --retry-delay",
                '"--retries", type=page_flow.at_least(0)' in api
                and '"--retry-delay", type=page_flow.at_least(0)' in api)
    return ok


def test_output_is_atomic_and_formula_safe():
    group("Output files are replaced atomically, with a sane mode")
    ok = True
    import output_writer
    rows = rows_of("LISTING_US")
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        finish_run(rows, prefix, "both", False, blocked=False,
                   stop_reason="completed", pages_requested=1,
                   pages_completed=1, start_url="u", final_url="u")
        before = {ext: open(prefix + ext, "rb").read()
                  for ext in (".json", ".csv", ".meta.json")}

        real_dump, real_writer = json.dump, csv_module.DictWriter

        def broken_dump(obj, f, **k):
            f.write('[{"partial')
            raise OSError("disk full (injected)")

        class BrokenWriter(real_writer):
            def writerow(self, row):
                raise OSError("disk full (injected)")

        output_writer.json.dump = broken_dump
        try:
            ok &= check("a failing JSON write raises",
                        _raises(lambda: output_writer.write_json(rows, prefix + ".json")))
            ok &= check("a failing sidecar write raises",
                        _raises(lambda: output_writer.write_run_meta(prefix, {"x": 1})))
        finally:
            output_writer.json.dump = real_dump
        output_writer.csv.DictWriter = BrokenWriter
        try:
            ok &= check("a failing CSV write raises",
                        _raises(lambda: output_writer.write_csv(rows, prefix + ".csv")))
        finally:
            output_writer.csv.DictWriter = real_writer
        for ext, data in before.items():
            ok &= check(f"...and the previous {ext} is intact",
                        open(prefix + ext, "rb").read() == data)
        ok &= check("no temporary file is left behind",
                    sorted(os.listdir(tmp)) == ["out.csv", "out.json", "out.meta.json"])

        for umask_value, want in ((0o022, 0o644), (0o002, 0o664), (0o077, 0o600)):
            path = os.path.join(tmp, f"new-{umask_value:o}.json")
            old = os.umask(umask_value)
            try:
                output_writer.write_json(rows, path)
            finally:
                os.umask(old)
            got = os.stat(path).st_mode & 0o777
            ok &= check(f"a new file under umask {umask_value:03o} is {want:o} "
                        f"(got {got:o})", got == want)
        os.chmod(prefix + ".json", 0o640)
        output_writer.write_json(rows, prefix + ".json")
        ok &= check("an existing file keeps its mode (640)",
                    os.stat(prefix + ".json").st_mode & 0o777 == 0o640)

    group("CSV cells cannot run as formulas; JSON is left alone")
    hostile = Product(sku="1", title="=HYPERLINK(\"http://x\",\"y\")",
                      location_note="+cmd", badges=["@SUM(A1)", "Premier Host"],
                      amenities=["Pool", "-x"], price_note="\tlead tab",
                      property_summary="-", price=-5.0, rating=8.6)
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "f")
        code = finish_run([hostile], prefix, "both", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, start_url="u", final_url="u",
                          extra={"results_total": 1})
        cell = list(csv_module.DictReader(open(prefix + ".csv", newline="")))[0]
        data = json.load(open(prefix + ".json"))[0]
        meta = json.load(open(prefix + ".meta.json"))
        ok &= check("= is escaped", cell["title"].startswith("'="))
        ok &= check("+ is escaped", cell["location_note"] == "'+cmd")
        ok &= check("a joined list is escaped on its first element",
                    cell["badges"].startswith("'@SUM"))
        ok &= check("a list whose LATER element is formula-shaped is not",
                    cell["amenities"] == "Pool | -x")
        ok &= check("a leading tab is escaped", cell["price_note"] == "'\tlead tab")
        ok &= check("a number stays a number (-5.0)", cell["price"] == "-5.0")
        ok &= check("JSON keeps the site's bytes",
                    data["title"].startswith("=") and data["badges"][0] == "@SUM(A1)")
        ok &= check("the sidecar counts the escaped cells (5)",
                    meta.get("csv_cells_escaped") == 5 and code == 0)
        ok &= check("the caller's extra still lands in the sidecar",
                    meta.get("results_total") == 1)
        finish_run([hostile], prefix, "both", False, blocked=False,
                   stop_reason="completed", pages_requested=1,
                   pages_completed=1, start_url="u", final_url="u",
                   extra={"csv_cells_escaped": "caller"})
        meta = json.load(open(prefix + ".meta.json"))
        ok &= check("the count merges UNDER a caller's extra",
                    meta.get("csv_cells_escaped") == "caller")
    return ok


def test_no_private_citations():
    group("Shipped files state their rules instead of citing private notes")
    # The rules behind this code are written out where they apply. A pointer
    # to a document the reader cannot open (an internal notes file, or a
    # bare section number from one) explains nothing, so it may not come
    # back. The tokens are assembled from pieces so this check does not
    # match its own source.
    import subprocess
    banned = [re.compile(re.escape("CLAUDE" + ".md"), re.I),
              re.compile(re.escape("claude_" + "2scraper"), re.I),
              re.compile(re.escape("2scraper_" + "repo_rules"), re.I),
              re.compile("(?:play" + "book|tem" + "plate) " + chr(0xA7), re.I),
              re.compile(chr(0xA7) + r"\s*\d")]
    try:
        listed = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                                capture_output=True, text=True, check=True).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        listed = [os.path.relpath(os.path.join(d, f), REPO_ROOT)
                  for d, _, names in os.walk(REPO_ROOT) if ".git" not in d
                  for f in names]
    hits = []
    for rel in listed:
        path = os.path.join(REPO_ROOT, rel)
        if not os.path.isfile(path):
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(b.search(line) for b in banned):
                hits.append(f"{rel}:{lineno}")
    ok = check(f"no tracked file cites private notes or section numbers "
               f"{hits[:5] if hits else ''}", not hits and len(listed) > 10)
    return ok


# ---------------------------------------------------------------------------
# Proxy traffic savers: the pre-check and media blocking
# ---------------------------------------------------------------------------
_SAVERS_URL = ("https://www.vrbo.com/search?destination=Orlando,%20Florida,"
               "%20United%20States%20of%20America")
_SAVERS_SECRET = "s3cret-Pw-not-real"


class _StubProxy:
    """A one-port HTTP proxy on 127.0.0.1 that answers every request with a
    fixed status and records the request heads it received."""

    def __init__(self, status):
        import socket
        import threading
        self.status, self.heads = status, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.heads.append(data.decode("latin-1"))
                reason = {407: "Proxy Authentication Required",
                          402: "Payment Required", 200: "OK"}.get(
                              self.status, "X")
                conn.sendall(f"HTTP/1.1 {self.status} {reason}\r\n"
                             "Content-Length: 0\r\nConnection: close\r\n\r\n"
                             .encode())

    def url(self, secret=_SAVERS_SECRET):
        # Concatenated, so no line here looks like a credentialled URL.
        return "http://user-zone-x:" + secret + "@127.0.0.1:" + str(self.port)

    def close(self):
        self.sock.close()


def _closed_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def _captured_output():
    """stdout and every log record, as one string."""
    import logging
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with contextlib.redirect_stdout(buf):
            yield buf
    finally:
        root.removeHandler(handler)


class _FakeRoute:
    def __init__(self, resource_type):
        self.request = type("R", (), {"resource_type": resource_type})()
        self.verdict = None

    def abort(self):
        self.verdict = "abort"

    def continue_(self):
        self.verdict = "continue"


def _media_verdicts(handler):
    out = {}
    for rtype in ("image", "media", "font", "document", "script", "xhr",
                  "fetch", "stylesheet"):
        route = _FakeRoute(rtype)
        handler(route)
        out[rtype] = route.verdict
    return out


def _engine_args(engine, tmp, proxy_url, *extra):
    """The engine's own parser, on a stubbed argv, with no .env in play."""
    from unittest import mock
    argv = [engine.__name__, "--url", _SAVERS_URL, "--out",
            os.path.join(tmp, "out")]
    if proxy_url:
        argv += ["--proxy", proxy_url]
    argv += list(extra)
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(env_config, "load_env", lambda *a, **k: None):
        return engine.parse_args()


def test_proxy_traffic_savers(skips):
    group("Proxy traffic savers: pre-check before the browser, media "
          "blocking through a proxy")
    import argparse
    import asyncio
    from unittest import mock
    import output_writer
    import proxy_check
    ok = True

    # -- the pre-check, against a stub proxy --------------------------------
    stub = _StubProxy(407)
    try:
        res = proxy_check.precheck(stub.url(), _SAVERS_URL, timeout=5)
        ok &= check("a 407 from the proxy is auth_failed and fatal "
                    f"({res.outcome})", res.outcome == "auth_failed"
                    and res.fatal and res.stop_reason == "proxy_auth_failed")
        head = stub.heads[-1] if stub.heads else ""
        ok &= check("the pre-check is ONE CONNECT to the target host, with "
                    "the credential", len(stub.heads) == 1
                    and head.startswith("CONNECT www.vrbo.com:443 ")
                    and "Proxy-Authorization: Basic " in head)
        with _captured_output() as buf:
            gated = proxy_check.gate(ProxyPool([stub.url()]), _SAVERS_URL,
                                     timeout=5)
        text = buf.getvalue()
        ok &= check("gate() returns the fatal check on a 407",
                    gated is not None and gated.fatal)
        ok &= check("the 407 message names the variable and the cause",
                    proxy_check.PROXY_SOURCES.split()[0] in text
                    and "refused the credential" in text
                    and "traffic balance is exhausted" in text)
        ok &= check("the 407 message never prints the credential",
                    _SAVERS_SECRET not in text and "user-zone-x" not in text)
    finally:
        stub.close()

    stub = _StubProxy(402)
    try:
        res = proxy_check.precheck(stub.url(), _SAVERS_URL, timeout=5)
        msg = proxy_check.failure_message(res, stub.url())
        ok &= check("a 402 is balance_exhausted and fatal",
                    res.outcome == "balance_exhausted" and res.fatal
                    and res.stop_reason == "proxy_balance_exhausted")
        ok &= check("the 402 message says traffic/balance exhausted, no "
                    "credential", "traffic/balance exhausted" in msg
                    and _SAVERS_SECRET not in msg)
    finally:
        stub.close()

    stub = _StubProxy(200)
    try:
        res = proxy_check.precheck(stub.url(), "http://127.0.0.1:9/x",
                                   timeout=5)
        head = stub.heads[-1] if stub.heads else ""
        ok &= check("a plain-http target is one HEAD for its robots.txt",
                    res.outcome == "ok" and not res.fatal
                    and head.startswith("HEAD http://127.0.0.1:9/robots.txt "))
    finally:
        stub.close()

    dead = "http://u:" + _SAVERS_SECRET + "@127.0.0.1:" + str(_closed_port())
    res = proxy_check.precheck(dead, _SAVERS_URL, timeout=5)
    ok &= check("a proxy that does not answer is a transport error, NOT "
                "fatal (existing retry behaviour applies)",
                res.outcome == "transport_error" and not res.fatal)
    with _captured_output() as buf:
        gated = proxy_check.gate(ProxyPool([dead]), _SAVERS_URL, timeout=5)
    ok &= check("gate() lets a transport error through, credential unprinted",
                gated is None and _SAVERS_SECRET not in buf.getvalue())
    ok &= check("no pool means no pre-check",
                proxy_check.gate(None, _SAVERS_URL) is None)
    ok &= check("a socks5 exit is skipped, not refused",
                proxy_check.precheck("socks5://127.0.0.1:1", _SAVERS_URL)
                .outcome == "skipped")

    # -- the attempt sidecar ------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        for name, body in (("out.json", "[1]"), ("out.meta.json", "{}")):
            with open(os.path.join(tmp, name), "w") as f:
                f.write(body)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = output_writer.finish_without_fetch(
                out, stop_reason="proxy_auth_failed", pages_requested=3,
                start_url=_SAVERS_URL)
        with open(out + ".latest_attempt.meta.json") as f:
            meta = json.load(f)
        untouched = (open(out + ".json").read() == "[1]"
                     and open(out + ".meta.json").read() == "{}")
    ok &= check(f"a refused proxy exits 5 ({rc})", rc == 5)
    ok &= check("...and records status failed / proxy_auth_failed in the "
                "attempt sidecar", meta.get("status") == "failed"
                and meta.get("stop_reason") == "proxy_auth_failed"
                and meta.get("pages_completed") == 0)
    ok &= check("...leaving the previous output and its .meta.json alone",
                untouched)

    # -- when media is blocked ----------------------------------------------
    pool = ProxyPool(["http://127.0.0.1:1"])
    ns = argparse.Namespace
    ok &= check("through a local proxy, blocking follows the site default",
                proxy_check.block_media(ns(load_media=False, block_media=False),
                                        pool, remote=False)
                is proxy_check.BLOCK_MEDIA_BY_DEFAULT)
    ok &= check("not blocked without a proxy",
                not proxy_check.block_media(
                    ns(load_media=False, block_media=False), None, False))
    ok &= check("not blocked over --cdp-endpoint, even when forced",
                not proxy_check.block_media(
                    ns(load_media=False, block_media=True), pool, True))
    ok &= check("--load-media turns it off",
                not proxy_check.block_media(
                    ns(load_media=True, block_media=False), pool, False))
    ok &= check("an env value of 0 does not count as --load-media",
                proxy_check.block_media(ns(load_media="0", block_media=False),
                                        pool, False)
                is proxy_check.BLOCK_MEDIA_BY_DEFAULT)
    ok &= check("the hidden --block-media forces it without a proxy",
                proxy_check.block_media(ns(load_media=False, block_media=True),
                                        None, False))
    parser = argparse.ArgumentParser()
    proxy_check.add_media_flags(parser)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["--load-media", "--block-media"])
        exclusive = False
    except SystemExit:
        exclusive = True
    ok &= check("--load-media and --block-media are mutually exclusive",
                exclusive)
    if proxy_check.BLOCK_MEDIA_BY_DEFAULT:
        ok &= check("--block-media is hidden from --help",
                    "--block-media" not in parser.format_help()
                    and "--load-media" in parser.format_help())
    else:
        ok &= check("blocking is opt-in here, so --block-media is in --help",
                    "--block-media" in parser.format_help()
                    and "--load-media" in parser.format_help())
    ok &= check("vrbo keeps media by default (image_url depends on it)",
                proxy_check.BLOCK_MEDIA_BY_DEFAULT is False)
    ok &= check(f"{proxy_check.LOAD_MEDIA_ENV} maps onto --load-media",
                env_config.ENV_KEYS.get(proxy_check.LOAD_MEDIA_ENV)
                == "load_media")

    # -- the route handler, on a stub page ----------------------------------
    ctx = mock.MagicMock()
    with _captured_output():
        proxy_check.install_playwright(ctx)
    pattern, handler = ctx.route.call_args[0]
    verdicts = _media_verdicts(handler)
    ok &= check(f"Playwright: route on every URL ({pattern})", pattern == "**/*")
    ok &= check("Playwright: image/media/font aborted, everything else "
                f"continues {verdicts}",
                all(verdicts[t] == "abort" for t in ("image", "media", "font"))
                and all(v == "continue" for t, v in verdicts.items()
                        if t not in ("image", "media", "font")))

    cdp = mock.MagicMock()
    cdp.send = mock.AsyncMock()
    page = mock.MagicMock()
    page.target.createCDPSession = mock.AsyncMock(return_value=cdp)

    async def _drive_pyppeteer():
        await proxy_check.install_pyppeteer(page)
        on_paused = cdp.on.call_args[0][1]
        on_paused({"requestId": "r1", "resourceType": "Image"})
        await asyncio.sleep(0)

    with _captured_output():
        asyncio.run(_drive_pyppeteer())
    calls = [c[0] for c in cdp.send.await_args_list]
    enable = next((c[1] for c in calls if c[0] == "Fetch.enable"), {})
    kinds = sorted(pt.get("resourceType") for pt in enable.get("patterns", []))
    ok &= check("pyppeteer: Fetch pauses ONLY Image/Media/Font, on its own "
                f"CDP session {kinds}", cdp.on.call_args[0][0] ==
                "Fetch.requestPaused" and kinds == ["Font", "Image", "Media"])
    ok &= check("pyppeteer: a paused request is failed, not continued",
                ("Fetch.failRequest", {"requestId": "r1",
                                       "errorReason": "BlockedByClient"})
                in calls)
    ok &= check("pyppeteer: never the removed Network.setRequestInterception",
                "setRequestInterception" not in inspect.getsource(
                    proxy_check.install_pyppeteer).split('"""')[-1])

    driver = mock.MagicMock()
    with _captured_output():
        installed = proxy_check.install_selenium(driver)
    sent = {c[0][0]: c[0][1] for c in driver.execute_cdp_cmd.call_args_list}
    urls = sent.get("Network.setBlockedURLs", {}).get("urls", [])
    ok &= check("Selenium: Network.setBlockedURLs with image/video/font "
                "patterns", installed and "*.png" in urls and "*.woff2" in urls
                and "*.mp4?*" in urls)
    ok &= check("Selenium: no pattern touches a document, script, JSON or "
                "stylesheet", not [u for u in urls if re.search(
                    r"\.(html?|js|json|css)\b", u)])
    broken = mock.MagicMock()
    broken.execute_cdp_cmd.side_effect = RuntimeError("no CDP")
    with _captured_output():
        ok &= check("Selenium: a driver that refuses CDP is not fatal",
                    proxy_check.install_selenium(broken) is False)

    # -- each engine, driven: no browser is started on a 407 ----------------
    for name in ENGINES:
        try:
            engine = __import__(name)
        except ImportError as e:
            skips.append(f"{name} proxy savers driving — {e}")
            print("  SKIP  %s proxy savers — %s" % (name, e))
            continue
        stub = _StubProxy(407)
        launched = []
        if name == "playwright_scraper":
            target = "sync_playwright"
        else:
            target = "_Session"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                args = _engine_args(engine, tmp, stub.url())
                with mock.patch.object(
                        engine, target,
                        side_effect=lambda *a, **k: launched.append(1)), \
                        _captured_output() as buf:
                    try:
                        rc = engine.scrape(args)
                    except Exception as e:  # noqa: BLE001 — fail BY NAME
                        rc = f"raised {type(e).__name__}"
                side = os.path.join(tmp, "out.latest_attempt.meta.json")
                meta = json.load(open(side)) if os.path.exists(side) else {}
                wrote_rows = any(os.path.exists(os.path.join(tmp, "out" + s))
                                 for s in (".json", ".csv", ".meta.json"))
        finally:
            stub.close()
        ok &= check(f"{name}: a 407 exits 5 without starting a browser "
                    f"(rc={rc}, launched={len(launched)})",
                    rc == 5 and not launched)
        ok &= check(f"{name}: ...with stop_reason proxy_auth_failed in the "
                    "attempt sidecar and no output written",
                    meta.get("stop_reason") == "proxy_auth_failed"
                    and meta.get("status") == "failed" and not wrote_rows)
        ok &= check(f"{name}: ...and the credential never reaches the log",
                    _SAVERS_SECRET not in buf.getvalue())

    # -- each engine installs blocking only through a local proxy -----------
    for name in ENGINES:
        try:
            engine = __import__(name)
        except ImportError:
            continue  # already recorded as a skip above
        for label, proxy, extra, want in (
                ("through a proxy", "http://127.0.0.1:1", (), None),
                ("with --load-media", "http://127.0.0.1:1",
                 ("--load-media",), False),
                ("without a proxy", None, (), False),
                ("forced, no proxy", None, ("--block-media",), True)):
            if want is None:
                want = proxy_check.BLOCK_MEDIA_BY_DEFAULT
            with tempfile.TemporaryDirectory() as tmp:
                args = _engine_args(engine, tmp, proxy, *extra)
            pool = ProxyPool([proxy]) if proxy else None
            got = _engine_installs_blocking(engine, args, pool)
            ok &= check(f"{name}: media blocking {label} = {want} (got {got})",
                        got is want)
    return ok


def _engine_installs_blocking(engine, args, pool):
    """Open the engine's local session on fakes; True if blocking went in."""
    from unittest import mock
    name = engine.__name__
    with _captured_output():
        if name == "playwright_scraper":
            pw = mock.MagicMock()
            browser = pw.chromium.launch.return_value
            browser.version = "120.0.0.0"
            ctx = browser.new_context.return_value
            engine._launch_local(pw, args, pool)
            return ctx.route.called
        if name == "selenium_scraper":
            with mock.patch.object(engine.webdriver, "Chrome") as chrome:
                driver = chrome.return_value
                driver.capabilities = {"browserVersion": "120.0.0.0"}
                engine._Session(args, pool).open()
            return any(c[0][0] == "Network.setBlockedURLs"
                       for c in driver.execute_cdp_cmd.call_args_list)
        page = mock.MagicMock()
        for attr in ("setUserAgent", "setViewport", "authenticate",
                     "setRequestInterception", "evaluateOnNewDocument",
                     "setExtraHTTPHeaders"):
            setattr(page, attr, mock.AsyncMock())
        cdp = mock.MagicMock()
        cdp.send = mock.AsyncMock()
        page.target.createCDPSession = mock.AsyncMock(return_value=cdp)
        browser = mock.MagicMock()
        browser.newPage = mock.AsyncMock(return_value=page)
        browser.version = mock.AsyncMock(return_value="HeadlessChrome/120.0")

        async def fake_launch(*a, **k):
            return browser
        bridge = engine._AsyncBridge()
        try:
            with mock.patch.object(engine, "launch", fake_launch):
                engine._Session(bridge, args, pool).open()
        finally:
            closer = getattr(bridge, "close", None)
            if closer:
                closer()
        return any(c[0][0] == "Fetch.enable" for c in cdp.send.await_args_list)



# ---------------------------------------------------------------------------
# Proxy traffic meter and budget
# ---------------------------------------------------------------------------

class _ByteCountingProxy:
    """A forward HTTP proxy on 127.0.0.1 that serves every plain-http GET
    itself with a body of a known size, and counts every byte it received
    and sent. The meter is judged against these two numbers."""

    def __init__(self, sizes):
        import socket
        import threading
        self.sizes = dict(sizes)            # path -> body length
        self.received = self.sent = 0
        # CONNECTs are the browser's own background connections (a system
        # Chromium phones home through its proxy too); they are not page
        # requests, this stub refuses them, and they are kept apart.
        self.connects = 0
        self.paths = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        import threading
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        from urllib.parse import urlparse
        with conn:
            buf = b""
            while True:
                while b"\r\n\r\n" not in buf:
                    try:
                        chunk = conn.recv(65536)
                    except OSError:
                        return
                    if not chunk:
                        return
                    buf += chunk
                head, _, buf = buf.partition(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                if lines[0].startswith("CONNECT "):
                    self.connects += 1
                    try:
                        conn.sendall(b"HTTP/1.1 403 Forbidden\r\n"
                                     b"Content-Length: 0\r\n\r\n")
                    except OSError:
                        pass
                    return
                self.received += len(head) + 4
                length = 0
                for line in lines[1:]:
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1])
                while len(buf) < length:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                self.received += length
                buf = buf[length:]
                path = urlparse(lines[0].split(" ")[1]).path or "/"
                self.paths.append(path)
                size = self.sizes.get(path, 0)
                ctype = ("text/html" if path == "/" else "text/css"
                         if path.endswith(".css") else
                         "application/javascript" if path.endswith(".js")
                         else "application/octet-stream")
                body = (b"<html><head><link rel=stylesheet href=/s.css>"
                        b"<script src=/a.js></script></head><body><p id=t>"
                        if path == "/" else b"")
                body = body + b"x" * max(0, size - len(body))
                if path == "/":
                    body = body[:-11] + b"</p></body>"
                status = b"200 OK" if path in self.sizes else b"404 Not Found"
                out = (b"HTTP/1.1 " + status + b"\r\nContent-Type: "
                       + ctype.encode() + b"\r\nContent-Length: "
                       + str(len(body)).encode()
                       + b"\r\nCache-Control: no-store\r\n\r\n" + body)
                try:
                    conn.sendall(out)
                except OSError:
                    return
                self.sent += len(out)

    @property
    def total(self):
        return self.received + self.sent

    def url(self):
        return "http://127.0.0.1:" + str(self.port)

    def close(self):
        self.sock.close()


def _base_exception(fn):
    try:
        fn()
    except BaseException as e:  # noqa: BLE001 — the point is to see it
        return e
    return None


def test_proxy_meter_units(skips):
    group("Proxy traffic meter: counting, the budget, the sidecar")
    import argparse
    from unittest import mock
    import output_writer
    import proxy_meter
    ok = True
    ns = argparse.Namespace

    # -- the meter itself ----------------------------------------------------
    m = proxy_meter.ProxyMeter(1)
    fired = []
    m.on_exceed(lambda: fired.append(1))
    m.add(600_000)
    m.add(400_000)
    ok &= check("1 MB budget: exactly 1,000,000 bytes is not over it",
                m.bytes == 1_000_000 and not m.exceeded and not fired)
    m.add(1)
    ok &= check("...one byte more is, and the refusal hook fires",
                m.exceeded and fired == [1])
    m.add(50)
    ok &= check("...the hook fires once, not per request", fired == [1])
    err = _base_exception(m.check)
    ok &= check("check() raises ProxyBudgetExceeded once over budget",
                isinstance(err, proxy_meter.ProxyBudgetExceeded))
    ok &= check("ProxyBudgetExceeded passes an `except Exception` (a retry "
                "loop cannot swallow it)",
                not issubclass(proxy_meter.ProxyBudgetExceeded, Exception))
    ok &= check("sidecar fields: bytes, budget in bytes, exceeded",
                m.sidecar() == {"proxy_bytes": 1_000_051,
                                "proxy_budget_bytes": 1_000_000,
                                "proxy_budget_exceeded": True})
    ok &= check(f"summary line ({m.summary()})",
                m.summary() == "Proxy traffic: 1.0 MB of 1.0 MB budget")
    late = []
    m.on_exceed(lambda: late.append(1))
    ok &= check("a hook added after the cap was crossed runs at once",
                late == [1])
    free = proxy_meter.ProxyMeter(0)
    free.add(10 ** 12)
    ok &= check("--proxy-budget-mb 0 means no cap (null budget in the "
                "sidecar)", not free.exceeded and _base_exception(free.check)
                is None and free.sidecar()["proxy_budget_bytes"] is None)

    # -- the flag and the environment ----------------------------------------
    parser = argparse.ArgumentParser()
    proxy_meter.add_budget_flag(parser)
    for bad in ("-1", "abc", "nan", "inf"):
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["--proxy-budget-mb", bad])
            refused = False
        except SystemExit as e:
            refused = e.code == 2
        ok &= check(f"--proxy-budget-mb {bad} is a usage error (exit 2)",
                    refused)
    ok &= check("--proxy-budget-mb 0 and 2.5 parse",
                parser.parse_args(["--proxy-budget-mb", "0"]).proxy_budget_mb
                == 0.0 and parser.parse_args(
                    ["--proxy-budget-mb", "2.5"]).proxy_budget_mb == 2.5)
    env = proxy_meter.BUDGET_ENV
    with mock.patch.dict(os.environ, {env: ""}):
        ok &= check("default budget is 10 MB",
                    proxy_meter.budget_mb(ns(proxy_budget_mb=None)) == 10.0
                    and proxy_meter.DEFAULT_BUDGET_MB == 10.0)
    with mock.patch.dict(os.environ, {env: "2.5"}):
        ok &= check(f"{env} sets the budget",
                    proxy_meter.budget_mb(ns(proxy_budget_mb=None)) == 2.5)
        ok &= check("...and the flag wins over it",
                    proxy_meter.budget_mb(ns(proxy_budget_mb=1.0)) == 1.0)
    with mock.patch.dict(os.environ, {env: "lots"}):
        ok &= check(f"an unparseable {env} stops the run instead of "
                    "silently using a default",
                    isinstance(_base_exception(lambda: proxy_meter.budget_mb(
                        ns(proxy_budget_mb=None))), SystemExit))
    if hasattr(env_config, "ENV_KEYS"):
        ok &= check(f"{env} maps onto --proxy-budget-mb",
                    env_config.ENV_KEYS.get(env) == "proxy_budget_mb")
    pool = ProxyPool(["http://127.0.0.1:1"])
    ok &= check("metered only through a local proxy: not without one, not "
                "over --cdp-endpoint",
                proxy_meter.start(ns(proxy_budget_mb=None), None) is None
                and proxy_meter.start(ns(proxy_budget_mb=None), pool,
                                      remote=True) is None
                and isinstance(proxy_meter.start(ns(proxy_budget_mb=None),
                                                 pool), proxy_meter.ProxyMeter))

    a = ns(proxy_budget_mb=0.001)
    proxy_meter.start(a, pool).add(5_000)
    ok &= check("proxy_meter.check(args) stops a run that is over budget",
                isinstance(_base_exception(lambda: proxy_meter.check(a)),
                           proxy_meter.ProxyBudgetExceeded)
                and _base_exception(lambda: proxy_meter.check(
                    ns(proxy_budget_mb=None))) is None)

    # -- CDP events with known sizes (pyppeteer, Selenium) ------------------
    m = proxy_meter.ProxyMeter(0)
    c = proxy_meter.CdpCounter(m)
    req = {"method": "GET", "url": "http://x.test/a", "headers": {"A": "b"}}
    est = len("GET http://x.test/a HTTP/1.1\r\n") + 2 + len("A: b\r\n")

    def _resp(rid, **extra):
        return {"requestId": rid,
                "response": dict({"url": "http://x.test/a", "status": 200},
                                 **extra)}
    c.feed("Network.requestWillBeSent", {"requestId": "1", "request": req})
    c.feed("Network.responseReceived", _resp("1"))
    c.feed("Network.dataReceived", {"requestId": "1", "encodedDataLength": 99})
    c.feed("Network.loadingFinished", {"requestId": "1",
                                       "encodedDataLength": 1234})
    ok &= check(f"CDP: a finished request = its request + encodedDataLength "
                f"({m.bytes} vs {est + 1234})", m.bytes == est + 1234)
    c.feed("Network.requestWillBeSent", {"requestId": "2", "request": req})
    c.feed("Network.loadingFailed", {"requestId": "2",
                                     "errorText": "net::ERR_BLOCKED_BY_CLIENT"})
    c.feed("Network.requestWillBeSent", {"requestId": "2b", "request": req})
    c.feed("Network.loadingFailed", {"requestId": "2b", "errorText": "x",
                                     "blockedReason": "inspector"})
    ok &= check("CDP: a request the browser blocked costs nothing",
                m.bytes == est + 1234)
    c.feed("Network.requestWillBeSent", {"requestId": "3", "request": req})
    c.feed("Network.responseReceived", _resp("3"))
    c.feed("Network.dataReceived", {"requestId": "3", "encodedDataLength": 50})
    c.feed("Network.loadingFailed", {"requestId": "3",
                                     "errorText": "net::ERR_CONNECTION_RESET"})
    ok &= check("CDP: a request that failed mid-body costs what arrived",
                m.bytes == 2 * est + 1234 + 50)
    c.feed("Network.requestWillBeSent", {"requestId": "4", "request": req})
    c.feed("Network.requestWillBeSent", {
        "requestId": "4", "request": req,
        "redirectResponse": {"encodedDataLength": 300}})
    c.feed("Network.responseReceived", _resp("4"))
    c.feed("Network.loadingFinished", {"requestId": "4",
                                       "encodedDataLength": 500})
    ok &= check("CDP: a redirect hop is counted as its own exchange",
                m.bytes == 4 * est + 1234 + 50 + 300 + 500)

    # Only what crossed the network. A sibling repo's Selenium run counted
    # Chrome's own new-tab page (chrome:// scripts, ~2.8 MB of
    # loadingFinished) while its proxy carried ~6 KB.
    before = m.bytes
    tab = {"method": "GET", "url": "chrome://new-tab-page/x.js",
           "headers": {"A": "b"}}
    c.feed("Network.requestWillBeSent", {"requestId": "t", "request": tab})
    c.feed("Network.responseReceived", {"requestId": "t", "response": {
        "url": "chrome://new-tab-page/x.js", "status": 200}})
    c.feed("Network.dataReceived", {"requestId": "t",
                                    "encodedDataLength": 2_779_683})
    c.feed("Network.loadingFinished", {"requestId": "t",
                                       "encodedDataLength": 2_779_683})
    for scheme_url in ("data:text/plain,x", "blob:http://x.test/1"):
        c.feed("Network.requestWillBeSent", {"requestId": scheme_url,
               "request": dict(req, url=scheme_url)})
        c.feed("Network.loadingFinished", {"requestId": scheme_url,
                                           "encodedDataLength": 900})
    ok &= check(f"CDP: a chrome:// new-tab load, data: and blob: cost nothing "
                f"({m.bytes - before} B)", m.bytes == before)
    c.feed("Network.requestWillBeSent", {"requestId": "k1", "request": req})
    c.feed("Network.requestServedFromCache", {"requestId": "k1"})
    c.feed("Network.responseReceived", _resp("k1"))
    c.feed("Network.loadingFinished", {"requestId": "k1",
                                       "encodedDataLength": 5000})
    for flag in ("fromDiskCache", "fromPrefetchCache", "fromServiceWorker"):
        c.feed("Network.requestWillBeSent", {"requestId": flag, "request": req})
        c.feed("Network.responseReceived", _resp(flag, **{flag: True}))
        c.feed("Network.loadingFinished", {"requestId": flag,
                                           "encodedDataLength": 5000})
    ok &= check(f"CDP: a cache hit (requestServedFromCache, fromDiskCache, "
                f"fromPrefetchCache, fromServiceWorker) costs nothing "
                f"({m.bytes - before} B)", m.bytes == before)
    c.feed("Network.requestWillBeSent", {"requestId": "e", "request": req})
    c.feed("Network.loadingFinished", {"requestId": "e",
                                       "encodedDataLength": 250_000})
    ok &= check(f"CDP: Chrome's own error page (no responseReceived) costs only "
                f"the request that was attempted ({m.bytes - before} B)",
                m.bytes - before == est)

    # -- Playwright: sizes() of every finished/failed request ---------------
    m = proxy_meter.ProxyMeter(0.001)
    ctx = mock.MagicMock()
    proxy_meter.install_playwright(ctx, m)
    on = {call[0][0]: call[0][1] for call in ctx.on.call_args_list}
    request = mock.MagicMock()
    request.sizes.return_value = {"requestHeadersSize": 100,
                                  "requestBodySize": 20,
                                  "responseHeadersSize": 200,
                                  "responseBodySize": 500}
    on.get("requestfinished", lambda r: None)(request)
    dead = mock.MagicMock()
    dead.sizes.side_effect = RuntimeError("no response")
    on.get("requestfailed", lambda r: None)(dead)
    ok &= check(f"Playwright: request.sizes() summed on requestfinished "
                f"({m.bytes} of 820), a failed one without sizes is 0",
                m.bytes == 820 and "requestfailed" in on)
    pattern, gate = ctx.route.call_args[0]
    route = mock.MagicMock()
    gate(route)
    ok &= check(f"Playwright: under budget the route falls through to the "
                f"next handler ({pattern})", pattern == "**/*"
                and route.fallback.called and not route.abort.called)
    on["requestfinished"](request)
    route = mock.MagicMock()
    gate(route)
    ok &= check("Playwright: over budget every new request is aborted",
                route.abort.called and not route.fallback.called)
    ok &= check("no meter, nothing installed",
                proxy_meter.install_playwright(ctx, None) is None
                and ctx.route.call_count == 1)

    # -- Selenium: the performance log --------------------------------------
    m = proxy_meter.ProxyMeter(0.001)
    driver = mock.MagicMock()
    log = [{"message": json.dumps({"message": {
        "method": "Network.requestWillBeSent",
        "params": {"requestId": "9", "request": req}}})},
        {"message": json.dumps({"message": {
            "method": "Network.responseReceived", "params": _resp("9")}})},
        {"message": json.dumps({"message": {
            "method": "Network.loadingFinished",
            "params": {"requestId": "9", "encodedDataLength": 2000}}})},
        {"message": "not json"}]
    driver.get_log.return_value = log
    proxy_meter.install_selenium(driver, m)
    entries = proxy_meter.selenium_log(driver)
    ok &= check(f"Selenium: the performance log is counted ({m.bytes} = "
                f"{est + 2000}) and handed back to the engine's own reader",
                m.bytes == est + 2000 and entries == log)
    blocked = [call[0] for call in driver.execute_cdp_cmd.call_args_list]
    ok &= check("Selenium: over budget, every URL is blocked over CDP",
                ("Network.setBlockedURLs", {"urls": ["*"]}) in blocked)
    options = mock.MagicMock()
    proxy_meter.selenium_options(options, m)
    ok &= check("Selenium: the performance log is requested",
                options.set_capability.call_args[0] ==
                ("goog:loggingPrefs", {"performance": "ALL"}))

    # -- pyppeteer: its own CDP session -------------------------------------
    import asyncio
    m = proxy_meter.ProxyMeter(0.001)
    cdp = mock.MagicMock()
    cdp.send = mock.AsyncMock()
    page = mock.MagicMock()
    page.target.createCDPSession = mock.AsyncMock(return_value=cdp)

    async def _drive():
        await proxy_meter.install_pyppeteer(page, m)
        on = {call[0][0]: call[0][1] for call in cdp.on.call_args_list}
        on["Network.requestWillBeSent"]({"requestId": "p", "request": req})
        on["Network.responseReceived"](_resp("p"))
        on["Network.loadingFinished"]({"requestId": "p",
                                       "encodedDataLength": 1500})
        await asyncio.sleep(0)

    asyncio.run(_drive())
    sent = [call[0] for call in cdp.send.await_args_list]
    ok &= check(f"pyppeteer: Network events counted ({m.bytes} = "
                f"{est + 1500})", m.bytes == est + 1500
                and ("Network.enable", {}) in sent)
    ok &= check("pyppeteer: over budget, every URL is blocked over CDP",
                ("Network.setBlockedURLs", {"urls": ["*"]}) in sent)

    # The cap can be crossed on another thread (a concurrent worker) that has
    # no event loop: the block must still reach this page's own loop.
    import threading
    m = proxy_meter.ProxyMeter(0.001)
    cdp = mock.MagicMock()
    cdp.send = mock.AsyncMock()
    page = mock.MagicMock()
    page.target.createCDPSession = mock.AsyncMock(return_value=cdp)
    crossed = []

    async def _drive_threaded():
        await proxy_meter.install_pyppeteer(page, m)
        worker = threading.Thread(target=lambda: crossed.append(
            _base_exception(lambda: m.add(5_000))))
        worker.start()
        worker.join()
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(_drive_threaded())
    sent = [call[0] for call in cdp.send.await_args_list]
    ok &= check(f"pyppeteer: a budget crossed on a worker thread still "
                f"blocks every URL on the page's loop (thread raised "
                f"{crossed})", crossed == [None]
                and ("Network.setBlockedURLs", {"urls": ["*"]}) in sent)

    # -- an HTTP client, through a real stub proxy with known sizes ---------
    import requests
    stub = _ByteCountingProxy({"/": 20_000})
    try:
        m = proxy_meter.ProxyMeter(0)
        r = requests.get("http://meter.test/", proxies={"http": stub.url()},
                         timeout=10)
        proxy_meter.count_response(m, r)
        import time as _time
        for _ in range(50):
            if stub.sent:
                break
            _time.sleep(0.02)
        drift = abs(m.bytes - stub.total) / stub.total
        ok &= check(f"HTTP client through a stub proxy: metered {m.bytes} B, "
                    f"the proxy carried {stub.total} B ({drift:.1%}, "
                    f"tolerance 5%)", drift <= 0.05)
    finally:
        stub.close()

    # -- the sidecars --------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        metered = proxy_meter.ProxyMeter(10)
        metered.add(123_456)
        rows = _meter_rows()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _meter_finish(rows, out, "completed", metered.sidecar())
        meta = json.load(open(out + ".meta.json"))
        attempt = json.load(open(out + ".latest_attempt.meta.json"))
        ok &= check(f"a metered run records proxy_bytes / proxy_budget_bytes "
                    f"/ proxy_budget_exceeded in .meta.json (rc={rc})",
                    rc == 0 and meta.get("proxy_bytes") == 123_456
                    and meta.get("proxy_budget_bytes") == 10_000_000
                    and meta.get("proxy_budget_exceeded") is False)
        ok &= check("...and the same in .latest_attempt.meta.json",
                    attempt.get("proxy_bytes") == 123_456
                    and attempt.get("status") == meta.get("status"))

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        over = proxy_meter.ProxyMeter(0.0001)
        over.add(5_000)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _meter_finish(_meter_rows(), out,
                               proxy_meter.STOP_BUDGET_EXCEEDED, over.sidecar())
        meta = json.load(open(out + ".meta.json"))
        ok &= check(f"over budget WITH rows: exit 6, partial, "
                    f"stop_reason proxy_budget_exceeded (rc={rc})",
                    rc == 6 and meta.get("status") == "partial"
                    and meta.get("stop_reason") == "proxy_budget_exceeded"
                    and meta.get("proxy_budget_exceeded") is True)

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        for name, body in (("out.json", "[1]"), ("out.meta.json", "{}")):
            with open(os.path.join(tmp, name), "w") as f:
                f.write(body)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _meter_finish([], out, proxy_meter.STOP_BUDGET_EXCEEDED,
                               over.sidecar())
        attempt = json.load(open(out + ".latest_attempt.meta.json"))
        untouched = (open(out + ".json").read() == "[1]"
                     and open(out + ".meta.json").read() == "{}")
        ok &= check(f"over budget with NO rows: exit 5 (rc={rc}), the "
                    f"attempt sidecar says so, the previous output is kept",
                    rc == 5 and untouched
                    and attempt.get("stop_reason") == "proxy_budget_exceeded"
                    and attempt.get("proxy_budget_exceeded") is True
                    and attempt.get("status") == "failed")

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        with contextlib.redirect_stdout(io.StringIO()):
            _meter_finish(_meter_rows(), out, "completed", None)
        ok &= check("an unmetered run writes no attempt sidecar and no "
                    "proxy fields (unchanged)",
                    not os.path.exists(out + ".latest_attempt.meta.json")
                    and "proxy_bytes" not in json.load(
                        open(out + ".meta.json")))

    with _captured_output() as buf:
        a = ns(proxy_budget_mb=None)
        setattr(a, "_proxy_meter", metered)
        extra = proxy_meter.finish(a, {"site": 1})
    ok &= check("the run ends with 'Proxy traffic: X.X MB of Y MB budget'",
                "Proxy traffic: 0.1 MB of 10.0 MB budget" in buf.getvalue()
                and extra.get("site") == 1 and extra.get("proxy_bytes"))
    return ok


# A request the PAGE makes after the cap. Navigations are stopped by the
# engines' own check() before they are issued; this is what the in-browser
# refusal (a route abort, CDP Network.setBlockedURLs) is for.
_LATE_FETCH = ("fetch('/n').then(r => r.status).catch(e => 'refused')")


def test_proxy_meter_in_a_real_browser(skips):
    """Opt-in (PROXY_METER_BROWSER=1): each installed driver loads a page
    through _ByteCountingProxy, and the meter must agree with what the proxy
    carried. Needs a Chromium; not run in CI, which installs none."""
    group("Proxy traffic meter in a real browser (opt-in)")
    if os.environ.get("PROXY_METER_BROWSER") != "1":
        print("  not run — set PROXY_METER_BROWSER=1 with a Chromium "
              "installed to measure the meter against a stub proxy")
        return True
    import proxy_meter
    import time as _time
    ok = True
    sizes = {"/": 60_000, "/s.css": 30_000, "/a.js": 90_000, "/n": 40_000}
    chromium = os.environ.get("PROXY_METER_CHROMIUM", "/usr/bin/chromium")
    target = "http://meter.test/"
    ran = 0
    for driver in ("playwright", "pyppeteer", "selenium"):
        try:
            __import__(driver)
        except ImportError:
            continue
        ran += 1
        stub = _ByteCountingProxy(sizes)
        meter = proxy_meter.ProxyMeter(0.15)
        stopped = None
        try:
            if driver == "playwright":
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    b = pw.chromium.launch(headless=True,
                                           proxy={"server": stub.url()})
                    ctx = b.new_context()
                    proxy_meter.install_playwright(ctx, meter)
                    p = ctx.new_page()
                    p.goto(target)
                    p.wait_for_timeout(800)
                    first = (meter.bytes, stub.total)
                    p.evaluate(_LATE_FETCH)
                    p.wait_for_timeout(500)
                    stopped = _base_exception(meter.check)
                    b.close()
            elif driver == "pyppeteer":
                import asyncio
                from pyppeteer import launch

                async def go():
                    b = await launch(headless=True, executablePath=chromium,
                                     args=["--no-sandbox",
                                           f"--proxy-server={stub.url()}"],
                                     handleSIGINT=False, handleSIGTERM=False,
                                     handleSIGHUP=False)
                    p = await b.newPage()
                    await proxy_meter.install_pyppeteer(p, meter)
                    await p.goto(target)
                    await asyncio.sleep(0.8)
                    snap = (meter.bytes, stub.total)
                    await p.evaluate(_LATE_FETCH, force_expr=True)
                    await asyncio.sleep(0.5)
                    await b.close()
                    return snap
                first = asyncio.new_event_loop().run_until_complete(go())
                stopped = _base_exception(meter.check)
            else:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.chrome.service import Service
                o = Options()
                o.add_argument("--headless=new")
                o.add_argument("--no-sandbox")
                o.add_argument(f"--proxy-server={stub.url()}")
                o.binary_location = chromium
                proxy_meter.selenium_options(o, meter)
                d = webdriver.Chrome(options=o, service=Service(
                    os.environ.get("PROXY_METER_CHROMEDRIVER",
                                   "/usr/bin/chromedriver")))
                proxy_meter.install_selenium(d, meter)
                d.get(target)
                _time.sleep(0.8)
                proxy_meter.drain_selenium(d)
                first = (meter.bytes, stub.total)
                d.execute_script("return " + _LATE_FETCH)
                _time.sleep(0.5)
                proxy_meter.drain_selenium(d)
                stopped = _base_exception(meter.check)
                d.quit()
        except Exception as e:  # noqa: BLE001 — report by name
            ok &= check(f"{driver}: real-browser meter run ({e!r:.160})", False)
            stub.close()
            continue
        stub.close()
        metered, carried = first
        drift = abs(metered - carried) / max(1, carried)
        ok &= check(f"{driver}: page + css + js through the stub proxy: "
                    f"metered {metered} B, the proxy carried {carried} B "
                    f"({drift:.1%}, tolerance 10%)", drift <= 0.10)
        ok &= check(f"{driver}: past the 0.15 MB budget a request the page "
                    f"makes never reaches the proxy, and check() stops the "
                    f"run before its next navigation (proxy saw {stub.paths})",
                    isinstance(stopped, proxy_meter.ProxyBudgetExceeded)
                    and "/a.js" in stub.paths and "/n" not in stub.paths)
    if not ran:
        print("  no browser driver installed")
    return ok


def _meter_rows():
    return rows_of("LISTING_US")[:3]


def _meter_finish(rows, out, stop_reason, proxy_extra):
    extra = {"site_fact": 1}
    if proxy_extra:
        extra.update(proxy_extra)
    return finish_run(rows, out, "json", False, blocked=False,
                      stop_reason=stop_reason, pages_requested=2,
                      pages_completed=1 if rows else 0,
                      start_url=_SAVERS_URL, final_url=_SAVERS_URL,
                      mode="listing", extra=extra)


def _engine_meter_installed(engine, args, pool):
    """Open the engine's local session on fakes; True if the meter went in."""
    from unittest import mock
    name = engine.__name__
    with _captured_output():
        if name == "playwright_scraper":
            pw = mock.MagicMock()
            browser = pw.chromium.launch.return_value
            browser.version = "120.0.0.0"
            ctx = browser.new_context.return_value
            engine._launch_local(pw, args, pool)
            return any(c[0][0] == "requestfinished" for c in ctx.on.call_args_list)
        if name == "selenium_scraper":
            with mock.patch.object(engine.webdriver, "Chrome") as chrome:
                driver = chrome.return_value
                driver.capabilities = {"browserVersion": "120.0.0.0"}
                driver.get_log.return_value = []
                engine._Session(args, pool).open()
            return getattr(driver, "_proxy_meter_counter", None) is not None \
                and not isinstance(driver._proxy_meter_counter, mock.MagicMock)
        page = mock.MagicMock()
        for attr in ("setUserAgent", "setViewport", "authenticate",
                     "setRequestInterception", "evaluateOnNewDocument",
                     "setExtraHTTPHeaders"):
            setattr(page, attr, mock.AsyncMock())
        cdp = mock.MagicMock()
        cdp.send = mock.AsyncMock()
        page.target.createCDPSession = mock.AsyncMock(return_value=cdp)
        browser = mock.MagicMock()
        browser.newPage = mock.AsyncMock(return_value=page)
        browser.version = mock.AsyncMock(return_value="HeadlessChrome/120.0")

        async def fake_launch(*a, **k):
            return browser
        bridge = engine._AsyncBridge()
        try:
            with mock.patch.object(engine, "launch", fake_launch):
                engine._Session(bridge, args, pool).open()
        finally:
            closer = getattr(bridge, "close", None)
            if closer:
                closer()
        return any(c[0][0] == "Network.enable" for c in cdp.send.await_args_list)


def _engine_session_mocks(engine):
    """Patch out everything in `scrape` that would start a browser."""
    from unittest import mock
    name = engine.__name__
    if name == "playwright_scraper":
        return [mock.patch.object(engine, "sync_playwright"),
                mock.patch.object(engine, "_BrowserSession")]
    if name == "puppeteer_scraper":
        return [mock.patch.object(engine, "_AsyncBridge"),
                mock.patch.object(engine, "_Session")]
    return [mock.patch.object(engine, "_Session")]


def _navigation_mock(engine, session):
    name = engine.__name__
    if name == "playwright_scraper":
        return session.page.goto
    if name == "puppeteer_scraper":
        return session.bridge.run
    return session.driver.get


def _drive_budget_run(engine, tmp, rows_on_page_1):
    """scrape() with the page fetch faked: page 1 spends past the budget,
    page 2 goes through the meter's check like a real fetch does."""
    import proxy_meter
    from unittest import mock
    args = _engine_args(engine, tmp, "http://127.0.0.1:" + str(_closed_port()),
                        "--pages", "3", "--proxy-budget-mb", "0.01",
                        "--delay", "0", "--format", "json")
    calls = []

    def fake_fetch(session, a, pool, page_num, url):
        calls.append(page_num)
        proxy_meter.check(a)
        proxy_meter.of(a).add(20_000)
        if not rows_on_page_1:
            proxy_meter.check(a)
        return engine.PageOutcome(page_num=page_num, url=url or _SAVERS_URL,
                                  final_url=_SAVERS_URL,
                                  products=_meter_rows(), state="ok")

    with contextlib.ExitStack() as stack:
        for patch in _engine_session_mocks(engine):
            stack.enter_context(patch)
        stack.enter_context(mock.patch.object(engine, "_fetch_one_page",
                                              side_effect=fake_fetch))
        stack.enter_context(mock.patch.object(engine.time, "sleep"))
        buf = stack.enter_context(_captured_output())
        try:
            rc = engine.scrape(args)
        except BaseException as e:  # noqa: BLE001 — fail BY NAME
            rc = f"raised {type(e).__name__}"
    out = os.path.join(tmp, "out")
    meta = (json.load(open(out + ".meta.json"))
            if os.path.exists(out + ".meta.json") else {})
    attempt = (json.load(open(out + ".latest_attempt.meta.json"))
               if os.path.exists(out + ".latest_attempt.meta.json") else {})
    return rc, calls, meta, attempt, buf.getvalue()


def test_proxy_meter_in_the_engines(skips):
    group("Proxy traffic meter: wired into every engine")
    import proxy_meter
    from unittest import mock
    ok = True
    for name in ENGINES:
        try:
            engine = __import__(name)
        except ImportError as e:
            skips.append(f"{name} proxy meter driving — {e}")
            print("  SKIP  %s proxy meter — %s" % (name, e))
            continue
        for label, proxy, extra, want in (
                ("through a proxy", "http://127.0.0.1:1", (), True),
                ("without a proxy", None, (), False)):
            with tempfile.TemporaryDirectory() as tmp:
                args = _engine_args(engine, tmp, proxy, *extra)
            pool = ProxyPool([proxy]) if proxy else None
            proxy_meter.start(args, pool, remote=False)
            got = _engine_meter_installed(engine, args, pool)
            ok &= check(f"{name}: meter installed {label} = {want} (got {got})",
                        got is want)

        # The real page fetch refuses to navigate once over budget.
        with tempfile.TemporaryDirectory() as tmp:
            args = _engine_args(engine, tmp, "http://127.0.0.1:1",
                                "--proxy-budget-mb", "0.001")
        meter = proxy_meter.start(args, ProxyPool(["http://127.0.0.1:1"]))
        meter.add(5_000)
        session = mock.MagicMock()
        session.driver.get_log.return_value = []
        with _captured_output():
            err = _base_exception(lambda: engine._fetch_one_page(
                session, args, None, 1, _SAVERS_URL))
        ok &= check(f"{name}: over budget, the page fetch stops before "
                    f"navigating ({type(err).__name__})",
                    isinstance(err, proxy_meter.ProxyBudgetExceeded)
                    and not _navigation_mock(engine, session).called)

        with tempfile.TemporaryDirectory() as tmp:
            rc, calls, meta, attempt, log = _drive_budget_run(engine, tmp, True)
        ok &= check(f"{name}: budget crossed on page 1 of 3 → exit 6, rows "
                    f"kept, no third page (rc={rc}, pages={calls})",
                    rc == 6 and calls == [1, 2] and meta.get("products") == 3)
        ok &= check(f"{name}: ...stop_reason proxy_budget_exceeded and the "
                    f"proxy fields in both sidecars",
                    meta.get("stop_reason") == "proxy_budget_exceeded"
                    and meta.get("proxy_budget_exceeded") is True
                    and meta.get("proxy_bytes") == 20_000
                    and meta.get("proxy_budget_bytes") == 10_000
                    and attempt.get("proxy_bytes") == 20_000)
        ok &= check(f"{name}: ...and the log says 'Proxy traffic: 0.0 MB of "
                    f"0.0 MB budget'", "Proxy traffic: 0.0 MB of 0.0 MB budget"
                    in log)
        with tempfile.TemporaryDirectory() as tmp:
            rc, calls, meta, attempt, log = _drive_budget_run(engine, tmp, False)
        ok &= check(f"{name}: budget crossed before any row → exit 5, no "
                    f"output, the attempt sidecar says why (rc={rc})",
                    rc == 5 and not meta
                    and attempt.get("stop_reason") == "proxy_budget_exceeded"
                    and attempt.get("proxy_bytes") == 20_000)
    return ok


def test_canary_reports_proxy_traffic():
    group("Canary: proxy traffic in the step summary, budget enforced")
    import subprocess
    ok = True
    text = open(os.path.join(REPO_ROOT, ".github", "workflows", "canary.yml"),
                encoding="utf-8").read()
    steps = re.split(r"\n\s*- (?:name|uses):", text)
    runs = [s for s in steps if re.search(r"\w+_scraper\.py", s)
            and "--out" in s]
    ok &= check(f"every canary scrape passes --proxy-budget-mb 10 "
                f"({len(runs)} run step(s))", bool(runs) and all(
                    "--proxy-budget-mb 10" in r for r in runs))
    outs = set()
    for r in runs:
        outs.update(re.findall(r"--out\s+(\S+)", r))
    reporters = [s for s in steps if ".github/proxy_traffic.py" in s
                 and "!cancelled()" in s]
    reported = set()
    for s in reporters:
        reported.update(re.findall(r"proxy_traffic\.py\s+\S+\s+(\S+)", s))
    ok &= check(f"each run's proxy traffic goes to the step summary, even "
                f"after a failed step (runs {sorted(outs)}, reported "
                f"{sorted(reported)})", bool(outs) and outs <= reported)

    script = os.path.join(REPO_ROOT, ".github", "proxy_traffic.py")
    with tempfile.TemporaryDirectory() as tmp:
        summary = os.path.join(tmp, "summary.md")
        results = {}
        for case, meta in (
                ("over", {"proxy_bytes": 12_300_000,
                          "proxy_budget_bytes": 10_000_000,
                          "proxy_budget_exceeded": True}),
                ("under", {"proxy_bytes": 2_340_000,
                           "proxy_budget_bytes": 10_000_000,
                           "proxy_budget_exceeded": False}),
                ("plain", {"status": "complete"}),
                ("missing", None)):
            out = os.path.join(tmp, case)
            if meta is not None:
                with open(out + ".latest_attempt.meta.json", "w") as f:
                    json.dump(meta, f)
            proc = subprocess.run(
                [sys.executable, script, "live/" + case, out],
                capture_output=True, text=True,
                env={**os.environ, "GITHUB_STEP_SUMMARY": summary})
            results[case] = (proc.returncode, proc.stdout)
        written = open(summary).read() if os.path.exists(summary) else ""
    rc, out = results["over"]
    ok &= check(f"over budget: '::error::stopped: proxy budget 10 MB "
                f"exceeded' and the step fails (rc={rc})", rc == 1
                and "::error::stopped: proxy budget 10 MB exceeded" in out)
    ok &= check("under budget: '**live/under**: proxy traffic 2.3 MB of "
                "10 MB' in the summary, step passes",
                results["under"][0] == 0
                and "**live/under**: proxy traffic 2.3 MB of 10 MB" in written
                and "**live/over**: proxy traffic 12.3 MB of 10 MB" in written)
    ok &= check("no proxy fields / no sidecar: reported, not a crash",
                results["plain"][0] == 0 and results["missing"][0] == 0
                and "not metered" in written and "unknown" in written)
    return ok


def main() -> int:
    ok = True
    skips = []

    ok &= test_numbers_and_prices()
    ok &= test_values_on_real_fixtures()
    ok &= test_urls()
    ok &= test_pagination()
    ok &= test_page_state()
    ok &= test_challenge_is_not_always_solvable()
    ok &= test_block_backoff_and_schema_version()
    ok &= test_retries_zero_is_refused()
    ok &= test_page_flow_policy()
    ok &= test_scroll_loop()
    ok &= test_throttle_is_not_completion()
    ok &= test_output_contract()
    ok &= test_writers_and_finish_run()
    ok &= test_diff()
    ok &= test_env_config()
    ok &= test_proxy_pool()
    ok &= test_credentials_never_reach_a_log()
    ok &= test_engine_parity(skips)
    ok &= test_no_undefined_names()
    ok &= test_dockerfile_matches_its_entrypoint()
    ok &= test_wording()
    ok &= test_no_capture_leaks()
    ok &= test_ci_checks_is_wired_up()
    ok &= test_sample_output()
    ok &= test_required_files_are_committed()
    ok &= test_readme_claims()
    ok &= test_x_debug_header_is_redacted()
    ok &= test_scraper_api_waitfor_is_an_object()
    ok &= test_engine_fetch_loop()
    ok &= test_numeric_flags_are_validated()
    ok &= test_output_is_atomic_and_formula_safe()
    ok &= test_no_private_citations()
    ok &= test_proxy_traffic_savers(skips)
    ok &= test_proxy_meter_units(skips)
    ok &= test_proxy_meter_in_the_engines(skips)
    ok &= test_proxy_meter_in_a_real_browser(skips)
    ok &= test_canary_reports_proxy_traffic()

    passed = _total_checks - len(_failures)
    if passed < CLAIMED_CHECK_FLOOR:
        ok = False
        _failures.append(
            f"the README and CHANGELOG claim over {CLAIMED_CHECK_FLOOR} "
            f"checks and only {passed} ran — either checks were removed or "
            f"the claim needs lowering")

    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d engine group(s) SKIPPED — an optional engine library is "
              "absent. CI's engine-smoke job installs each engine in its own "
              "venv and fails if this list is non-empty, because a skip reads "
              "exactly like a passing run:" % len(skips))
        for s in skips:
            print("  - %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
