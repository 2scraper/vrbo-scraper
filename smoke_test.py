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


def check(label, condition):
    """Print and record one check. Returns the condition so callers can
    accumulate with `ok &= check(...)`."""
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

    group("Currency, in §4's order of trust")
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
    group("VALUES on real fixtures, not coverage (§10)")
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

    group("The German storefront — the second-locale checks (§15)")
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

    group("Concurrency is refused WITH the reason (§18)")
    refusal = page_flow.concurrency_refusal("https://www.vrbo.com/search?destination=X")
    ok &= check("refused for a search", refusal is not None)
    ok &= check("the reason names the cause, not just 'unsupported'",
                refusal is not None and "no per-page address" in refusal)
    ok &= check("refused for a property page too",
                page_flow.concurrency_refusal("https://www.vrbo.com/pdp/lo/1") is not None)
    ok &= check("concurrency_limit is 1 everywhere",
                page_flow.concurrency_limit("https://www.vrbo.com/search?d=X") == 1)

    group("The counter is the completeness oracle (§8)")
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
    # of the site's own assets?" answers correctly (§18).
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
    # built real page from coming back blocked (§17's ordering trap).
    ok &= check("a served grid references the site's assets",
                served_by_vrbo(fixture("LISTING_US")))
    ok &= check("the browser error page does not",
                not served_by_vrbo(fixture("BROWSER_ERROR")))

    group("`akamai` is NOT a marker here, and that is the point (§18)")
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
    group("detected != blocking != paying (§8)")
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
    ok &= check("it leads with the browser, not the proxy",
                "channel=chrome" in advice)
    ok &= check("it says the vendor could not be solved and nothing was charged",
                "nothing was charged" in advice)
    ok &= check("it names the rate response as clearable",
                "RATE response" in advice)
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
    # A policy constant nothing reads is the same defect as dead code (§17).
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
    ok &= check("MIN_CARD_MATCHES is above 1 (§5)", page_flow.MIN_CARD_MATCHES > 1)
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
    group("The scroll — the inner container, and THREE stable rounds (§8)")
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
    group("The output contract (§9)")
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
                {"twocaptcha_key", "cdp_endpoint", "proxy", "url"})
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

    group("A COPIED .env.example must read as unset (§17)")
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
                # long way from its cause (§17).
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
    group("An EXCEPTION MESSAGE is a log (§8)")
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
    group("The three engines agree — flags, in BOTH directions (§17)")
    ok = True
    flagsets = {}
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        flagsets[engine] = set(re.findall(r'p\.add_argument\("(--[a-z0-9-]+)"', src))

    # The family contract (§9). Every engine must carry all of these.
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
        "playwright_scraper": {"--locale", "--fingerprint", "--fp-tags",
                               "--fp-country", "--browser-channel"},
        "selenium_scraper": {"--locale", "--fingerprint", "--fp-tags",
                             "--fp-country"},
        "puppeteer_scraper": {"--chromium-path"},
    }
    for engine, extra in documented_extra.items():
        actual = flagsets[engine] - contract
        ok &= check(f"{engine}'s extra flags are exactly the documented set",
                    actual == extra)

    group("Every shared-module call binds against the real signature (§17)")
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

    group("Every engine imports its driver at MODULE level (§10)")
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
    group("Names that resolve, not just parse (§10)")
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
    group("The Dockerfile COPY list against the import graph (§10)")
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
    group("Wording enforced by a test (§12)")
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
    group("Committed fixtures carry no credential-shaped material (§10)")
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
    group("One credential check, invoked from CI and from here (§17)")
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
                ".github/workflows/canary.yml", "tests/test_smoke.py")
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


def main() -> int:
    ok = True
    skips = []

    ok &= test_numbers_and_prices()
    ok &= test_values_on_real_fixtures()
    ok &= test_urls()
    ok &= test_pagination()
    ok &= test_page_state()
    ok &= test_challenge_is_not_always_solvable()
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
