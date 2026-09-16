"""product_parser.py — everything this repo knows about Vrbo.

This is the only module that is allowed to know what Vrbo's markup looks
like. The engines know a handful of named constants; `page_flow.py` knows the
policy that follows from what this module reports; everything else in the
repo is site-agnostic (CLAUDE.md §1).

Two things about this site shape the whole file, and both were measured
rather than assumed.

**There is no structured data on a listing page.** Zero
`application/ld+json` blocks, zero `__NEXT_DATA__`, and a
`window.__APOLLO_STATE__` holding three keys — a banner — because the grid
arrives over client-side POSTs to `/graphql`. So the primary path is the DOM
(§4's "the answer may be zero"), anchored on Vrbo's own `data-stid`
attributes rather than on the `uitk-…` class names, which are a design
system's and churn with it. A detail page DOES carry two JSON-LD blocks and
neither is a `Product`: a `BreadcrumbList` and an `FAQPage`. The breadcrumb
is read for `category`; the price still comes from the DOM.

**One brand, six storefronts, and they do not spell things the same way.**
Vrbo and its local siblings all run the same Expedia front end, and the same
element is reached by a different attribute depending on which one served
it: a card's price container is `data-stid="product-price-summary"` on
vrbo.com and `data-test-id="price-summary"` on fewo-direkt.de. A selector
naming one of them returns a null price on every row of the other site while
every other column looks fine — §15's "run a SECOND country site", earned
here within an hour of trying one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from output_writer import Product, SOURCE_DEFAULT


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------
# Vrbo publishes NO `<link rel="alternate" hreflang=...>` set anywhere —
# checked on every capture, on both a search page and a detail page — so
# §5's "take the host table from the site's own hreflang set" has nothing to
# take. Every host below was therefore fetched and checked instead, and each
# one's `siteid` is the Expedia site id its own pages carry, which is what
# tells the storefronts apart in the markup:
#
#   www.vrbo.com          9001001  en  United States        HTTP 200, 50 cards
#   www.vrbo.com/en-gb    9004006  en  United Kingdom       HTTP 200
#   www.fewo-direkt.de    9003020  de  Germany              HTTP 200, 50 cards
#   www.abritel.fr        9003013  fr  France               HTTP 200
#   www.bookabach.co.nz   9006043  en  New Zealand          HTTP 200
#   www.stayz.com.au      9005044  en  Australia            HTTP 200
#
# All six served the identical card markup (`data-stid="lodging-card-responsive"`)
# and the identical `?destination=&regionId=&sort=` convention. They are
# listed here because a Vrbo listing in Berlin is on fewo-direkt.de and
# nowhere else — refusing it would be refusing Vrbo's own inventory.
HOSTS: Tuple[str, ...] = (
    "vrbo.com",
    "fewo-direkt.de",
    "abritel.fr",
    "bookabach.co.nz",
    "stayz.com.au",
)

# What each storefront calls itself, for the `source` column. Keyed on the
# bare host so a `www.` prefix or its absence reads the same.
BRAND_BY_HOST: Dict[str, str] = {
    "vrbo.com": "vrbo.com",
    "fewo-direkt.de": "fewo-direkt.de",
    "abritel.fr": "abritel.fr",
    "bookabach.co.nz": "bookabach.co.nz",
    "stayz.com.au": "stayz.com.au",
}

# homeaway.com is deliberately NOT here. It redirects to vrbo.com rather than
# serving anything of its own, so listing it would advertise a host that is
# never the host a row came from.
REDIRECTS_TO_VRBO = ("homeaway.com", "homeaway.co.uk", "vacationrentals.com")


def site_host(url: str) -> Optional[str]:
    """The bare supported host this URL is on, or None."""
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host if host in HOSTS else None


def unsupported_reason(url: str) -> Optional[str]:
    """Why this URL is not one this scraper reads, or None if it is.

    Refused WITH the reason (§5): "is not a Vrbo site" is false for
    homeaway.com and sends the reader hunting for a typo that is not there.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return f"{url!r} is not an http(s) URL"
    host = (parts.hostname or "").lower()
    if not host:
        return f"{url!r} has no hostname"
    bare = host[4:] if host.startswith("www.") else host
    if bare in REDIRECTS_TO_VRBO:
        return (f"{host} redirects to vrbo.com and serves nothing of its "
                f"own — use the vrbo.com URL it lands on")
    if bare not in HOSTS:
        return (f"{host} is not one of this scraper's measured storefronts "
                f"({', '.join(HOSTS)})")
    return None


def is_supported_host(url: str) -> bool:
    return unsupported_reason(url) is None


def source_of(url: str) -> str:
    """The `source` column's value for a row from this URL."""
    return BRAND_BY_HOST.get(site_host(url) or "", SOURCE_DEFAULT)


# What each storefront quotes in when the page states no code of its own.
# Used ONLY as a last resort, and it exists because of one specific way to be
# wrong: stayz.com.au and bookabach.co.nz print a BARE `$`, and reading that
# as USD turns an A$18 property into an $18 one — a plausible number in the
# wrong currency, which is the worst kind (§4 ranks a bare symbol last for
# exactly this reason). The host is a fact about which storefront served the
# page, so it beats guessing from the symbol; the page's own `"currency"`
# config still beats both, and vrbo.com's currency picker is reflected there.
HOST_CURRENCY: Dict[str, str] = {
    "vrbo.com": "USD",
    "fewo-direkt.de": "EUR",
    "abritel.fr": "EUR",
    "bookabach.co.nz": "NZD",
    "stayz.com.au": "AUD",
}


# ---------------------------------------------------------------------------
# URL shapes
# ---------------------------------------------------------------------------
# Three id spaces live side by side in one grid, measured across 50 cards on
# one search page: 18 `/pdp/lo/N`, 25 `/N` and 7 `/Nha`. The first is
# Expedia-side lodging (hotels and aparthotels, which this site does return
# alongside whole-home rentals); the second is a Vrbo property id; the third
# is a HomeAway-legacy id that keeps its `ha` suffix. All three are the
# path's last segment, which is what the site's own link commits to, so that
# is what `sku` holds.
# A FIFTH shape, and only the second storefront found it (§15): the local
# brands route some properties through a localized SEO slug —
# `/ferienwohnung-ferienhaus/p5813635` on fewo-direkt.de,
# `/holiday-rental/p20304340` on stayz.com.au,
# `/holiday-accommodation/p20315976` on bookabach.co.nz — and vrbo.com never
# uses it at all. Eleven of fifty German cards took it, so a pattern written
# against vrbo.com alone reads `sku` as null on a fifth of every German run
# while every other column looks perfectly healthy.
_LOCALE_PREFIX = r"(?:[a-z]{2}-[a-z]{2}/)?"
_PDP_PATH_RE = re.compile(r"^/" + _LOCALE_PREFIX + r"pdp/lo/(?P<id>\d+)/?$", re.I)
_PROPERTY_PATH_RE = re.compile(
    r"^/" + _LOCALE_PREFIX + r"(?:[a-z][a-z-]*/p)?(?P<id>\d+(?:ha)?)/?$", re.I)
_SEARCH_PATH_RE = re.compile(r"^/" + _LOCALE_PREFIX + r"search/?$", re.I)

_SKU_IN_URL_RE = re.compile(
    r"^/" + _LOCALE_PREFIX + r"(?:pdp/lo/|[a-z][a-z-]*/p)?(?P<id>\d+(?:ha)?)/?$",
    re.I)

# Tracking and session parameters. Stripped before two URLs are compared,
# because a card's own href carries a dozen of them — including a `pwa_ts`
# millisecond timestamp and a per-load `searchId` — so two spellings of the
# same property differ on every load unless these go.
TRACKING_PARAMS = frozenset("""
    x_pwa rfrr pwa_ts referrerUrl useRewards searchId userIntent
    containsVideo dateless neighborhoodId destType latLong
    utm_source utm_medium utm_campaign utm_term utm_content
    mdpcid mdpdtl icmcid icmdtl gclid fbclid
""".split())


def strip_tracking(url: str) -> str:
    """The same address with tracking, session and fragment noise removed."""
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qs(parts.query, keep_blank_values=True).items()
            if k not in TRACKING_PARAMS]
    query = urlencode([(k, one) for k, many in kept for one in many])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def listing_kind(url: str) -> str:
    """Which kind of page this URL addresses: search, property, or unknown."""
    path = urlsplit(url).path or "/"
    if _SEARCH_PATH_RE.match(path):
        return "search"
    if _PDP_PATH_RE.match(path) or _PROPERTY_PATH_RE.match(path):
        return "property"
    return "unknown"


def sku_from_url(url: str) -> Optional[str]:
    """The property id in this URL's path, or None.

    The path's last segment, kept verbatim including a `ha` suffix. NOT
    `expediaPropertyId` from the query string: on a `/N` card those two are
    different numbers (path 2430840 against expediaPropertyId 70477069), so
    conflating them would give one property two identities.
    """
    match = _SKU_IN_URL_RE.match(urlsplit(url).path or "")
    return match.group("id") if match else None


def expedia_property_id_from_url(url: str) -> Optional[str]:
    """Expedia's own id for this property, from the card's query string."""
    values = parse_qs(urlsplit(url).query).get("expediaPropertyId") or []
    return values[0] or None if values else None


# ---------------------------------------------------------------------------
# Pagination — and why there is no page_url()
# ---------------------------------------------------------------------------
# A Vrbo search listing has NO per-page address, and it does not fail when
# you invent one: measured, with a real browser, on the same URL minutes
# apart —
#
#     ?…&startIndex=50   HTTP 200, counter still "1 - 50 of 300+", same cards
#     ?…&page=2          HTTP 200, counter still "1 - 50 of 300+", same cards
#
# So a `page_url()` built on either convention would fetch page 1 again, find
# no new sku, conclude the listing was exhausted and report a COMPLETE run
# holding a sixth of the catalogue (§18). There is deliberately no such
# function in this module: the only way to page 2 is to press the site's own
# button, which fires a `/graphql` POST and leaves the address bar untouched.
#
# Measured working: pressing `[data-stid="next-button"]` moved the counter
# from "1 - 50 of 300+" to "51 - 100 of 300+" with zero title overlap
# against page 1, and `window.location` never changed.
PAGINATES_BY_URL = False

# The button, and the counter beside it. Both are `data-stid`, which is the
# site's own test id rather than a build-hash class (§4).
NEXT_PAGE_SELECTOR = '[data-stid="next-button"]:not([disabled])'
PAGINATION_SELECTOR = '[data-stid="pagination-navigation"]'
RESULTS_HEADER_SELECTOR = '[data-stid="results-header-message"]'

# How deep this scraper will walk one listing. Not a site limit — the site
# advertises "300+" and keeps going — but a walk of N pages costs N sequential
# button presses and a fresh GraphQL round trip each, so a runaway `--pages`
# is a bill rather than a dataset.
PAGE_CAP = 40


def paginates_by_url(url: str) -> bool:
    """Whether page N of this listing has an address of its own.

    False everywhere on this site, for both page kinds. Stated as a function
    rather than read as a constant so the engines and page_flow ask the same
    question they ask in every other repo in this family.
    """
    return PAGINATES_BY_URL


SELECTORS: Dict[str, str] = {
    # A search result card. The readiness anchor and the row unit both.
    "item_card": '[data-stid="lodging-card-responsive"]',
    # The card's link to the property. `open-product-information` is the
    # whole-card overlay anchor; it is the one link a card has to its own
    # property, which matters because the tile-scoping failure §4 describes
    # starts with a card that links to itself twice.
    "item_link": '[data-stid="lodging-card-responsive"] a[data-stid="open-product-information"]',
    # The results list is an INNER scroller. The page body does not scroll at
    # all: `document.body.scrollHeight === window.innerHeight === 900` on
    # every capture, so §8's "scroll to document.body.scrollHeight" moves
    # nothing here — measured, 12 window scrolls, 18 cards before and 18
    # after. Scrolling THIS element reached 50 of 50 in two rounds.
    "scroll_container": ".scrollable-result-section",
    # Two spellings of one element, and both are real (see the module
    # docstring). Kept as one comma selector so every read gets both.
    "card_price": '[data-stid="product-price-summary"], [data-test-id="price-summary"]',
    "card_title": "h3.uitk-heading-5",
    "card_badge": "span.uitk-badge-base-text",
    "card_summary": ".truncate-lines-3, .truncate-lines-2",
    "card_location_note": '[data-stid="featured-messages-container"]',
    "card_amenities": '[data-stid="amenity-highlights-with-icons"]',
    "card_image": "img.uitk-image-media",
    "a11y": ".is-visually-hidden",
    # Detail page.
    "detail_title": "h1",
    "detail_address": '[data-stid="content-hotel-address"]',
    "detail_reviews": '[data-stid="content-hotel-reviewsummary"]',
    "detail_price": '[data-test-id="price-summary"], [data-stid="product-price-summary"]',
}


# ---------------------------------------------------------------------------
# Numbers, in six locales
# ---------------------------------------------------------------------------
# The German storefront is why this is not `float(text)`:
#
#     rating        8.6                 vs   8,0
#     reviews       (1,299 reviews)     vs   (1.614 bewertungen)
#     price         $81                 vs   66 €
#     counter       1 - 50 of 300+      vs   1–50 von >300
#
# `int("1.614")` raises and `re.search(r"\d+", "1.614")` returns 1 — the
# amazon-scraper `review_count` bug (§10) in a different costume. Everything
# numeric on this site goes through `_normalize_amount`.
_GROUP_SPACES = "    "
_AMOUNT = (r"\d{1,3}(?:[.,    ]\d{3})+(?:[.,]\d{1,2})?"
           r"|\d+(?:[.,]\d{1,2})?")

# Currency symbols this family of storefronts actually prints, longest first
# so `A$` is not swallowed by a bare `$` (§4). The bare `$` is last and means
# USD, because vrbo.com is the only storefront here that prints it alone.
CURRENCY_SYMBOLS: Tuple[Tuple[str, str], ...] = (
    ("NZ$", "NZD"), ("A$", "AUD"), ("CA$", "CAD"), ("C$", "CAD"),
    ("US$", "USD"), ("€", "EUR"), ("£", "GBP"), ("CHF", "CHF"), ("$", "USD"),
)

_SYMBOL_ALTERNATION = "|".join(
    re.escape(symbol) for symbol, _ in CURRENCY_SYMBOLS)
_PRICE_RE = re.compile(
    r"(?:" + _SYMBOL_ALTERNATION + r")\s*(" + _AMOUNT + r")"
    r"|(" + _AMOUNT + r")\s*(?:" + _SYMBOL_ALTERNATION + r")")
_PCT_RE = re.compile(r"-?\s*\d{1,3}(?:[.,]\d+)?\s*%|-?\s*%\s*\d{1,3}(?:[.,]\d+)?")
_BARE_NUMBER_RE = re.compile(_AMOUNT)

# Real ISO 4217 codes this family's storefronts quote in. An allowlist rather
# than `[A-Z]{3}`, so a three-letter word in a property title cannot become a
# currency (§4).
ISO_CURRENCIES = frozenset((
    "USD", "EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "SEK", "NOK", "DKK",
    "PLN", "CZK", "HUF", "JPY", "MXN", "BRL", "ZAR", "SGD", "HKD", "THB",
))

_PAGE_CURRENCY_RE = re.compile(r'currency\\*"\s*:\s*\\*"([A-Z]{3})\\*"')


def _normalize_amount(raw: str) -> Optional[float]:
    """A written number as a float, honouring all three groupings.

    `1,234.56` / `1.234,56` / `1 234,56`, the space form allowing NBSP,
    narrow NBSP and thin space — a rendered page uses a no-break variant so
    the number does not wrap, and missing them parses `1 234 €` as 234 (§4).

    Whichever of dot and comma comes LAST is the decimal point. Where only
    one appears, exactly three trailing digits is a thousands grouping,
    because no currency has a three-digit subunit — which is what makes
    `1.614 Bewertungen` 1614 and `8,0 von 10` 8.0.
    """
    text = (raw or "").strip()
    for space in _GROUP_SPACES:
        if space != " ":
            text = text.replace(space, " ")
    text = text.replace(" ", ".")
    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif has_comma:
        head, _, tail = text.rpartition(",")
        text = (head + tail) if len(tail) == 3 else text.replace(",", ".")
    elif has_dot:
        head, _, tail = text.rpartition(".")
        if len(tail) == 3:
            text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


def numbers_in(text: str) -> List[float]:
    """Every written number in `text`, in reading order, grouping honoured."""
    out: List[float] = []
    for match in _BARE_NUMBER_RE.finditer(text or ""):
        value = _normalize_amount(match.group(0))
        if value is not None:
            out.append(value)
    return out


def int_in(text: str) -> Optional[int]:
    """The first written integer in `text`.

    Grouping-aware, which is the whole point: `(1.614 bewertungen)` is 1614,
    not 1.
    """
    found = numbers_in(text)
    if not found:
        return None
    return int(round(found[0]))


def prices_in(text: str) -> List[float]:
    """Every amount carrying a currency symbol, percentages removed FIRST.

    Removed first rather than rejected afterwards: a rejected match has
    already consumed the symbol, so skipping it loses the real price too
    (§4).
    """
    out: List[float] = []
    for match in _PRICE_RE.finditer(_PCT_RE.sub(" ", text or "")):
        value = _normalize_amount(match.group(1) or match.group(2))
        if value is not None:
            out.append(value)
    return out


def price_in(text: str) -> Optional[float]:
    found = prices_in(text)
    return found[0] if found else None


def currency_in(text: str, url: str = "") -> Optional[str]:
    """The currency a written amount wears, or None.

    The weak sources, in §4's own order: a written ISO code names itself; a
    PREFIXED symbol (`A$`, `NZ$`, `US$`) is nearly as good and is matched
    longest-first so it is not swallowed by the bare one; a bare `$` is a
    guess, and is resolved from the HOST rather than defaulted to USD — a
    storefront is a fact, `$` on stayz.com.au is not dollars from Delaware.
    Null when nothing says anything, never a defaulted "USD".
    """
    cleaned = _PCT_RE.sub(" ", text or "")
    for token in re.findall(r"\b([A-Z]{3})\b", cleaned):
        if token in ISO_CURRENCIES:
            return token
    for symbol, code in CURRENCY_SYMBOLS:
        if symbol == "$":
            continue
        if symbol in cleaned:
            return code
    if "$" in cleaned:
        return HOST_CURRENCY.get(site_host(url) or "")
    return None


def page_currency(html: str) -> Optional[str]:
    """The ISO code this page states for itself, or None.

    The strongest source available here, and it is a FACT rather than a
    reading of a symbol: every storefront's page config carries
    `"currency":"USD"` (escaped inside a JSON string literal, hence the
    backslash tolerance). Measured USD on vrbo.com and EUR on
    fewo-direkt.de. Never overwritten from a symbol on the tile.
    """
    for code in _PAGE_CURRENCY_RE.findall(html or ""):
        if code in ISO_CURRENCIES:
            return code
    return None


# ---------------------------------------------------------------------------
# How big is this listing, and did we get all of it?
# ---------------------------------------------------------------------------
@dataclass
class ResultsRange:
    """What the site says this page holds, from its own counter.

    `1 - 50 of 300+` on vrbo.com and `1–50 von >300` on fewo-direkt.de: an
    EN DASH instead of a hyphen and the floor marker on the other side of the
    number, which is why this is parsed as "the integers in that text" rather
    than by a phrase.
    """
    first: Optional[int] = None
    last: Optional[int] = None
    total: Optional[int] = None
    total_is_floor: bool = False

    @property
    def expected_on_page(self) -> Optional[int]:
        if self.first is None or self.last is None:
            return None
        return max(0, self.last - self.first + 1)


_FLOOR_MARKER_RE = re.compile(r"[+>]|\bmehr als\b|\bmore than\b|\bplus de\b",
                              re.I)


def results_range(html: str) -> ResultsRange:
    """Parse the pagination counter.

    This is the completeness oracle for a run, and it is arithmetic rather
    than a threshold (§8, the amazon rank gap): the page states that it holds
    items 1 to 50, so merging 40 rows off it is proof that ten cards never
    loaded. One measured run did exactly that — page 2 read `51 - 100 of
    300+` and yielded 40 cards — and without this counter the run would have
    looked healthy.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    node = soup.select_one(PAGINATION_SELECTOR)
    if node is None:
        return ResultsRange()
    text = node.get_text(" ", strip=True)
    found = [int(round(n)) for n in numbers_in(text)]
    floor = bool(_FLOOR_MARKER_RE.search(text))
    if len(found) >= 3:
        return ResultsRange(found[0], found[1], found[2], floor)
    if len(found) == 2:
        return ResultsRange(found[0], found[1], None, floor)
    return ResultsRange(total_is_floor=floor)


def total_results(html: str) -> Optional[int]:
    """How many properties the listing says it has in total."""
    return results_range(html).total


def missing_on_page(html: str, parsed: int) -> int:
    """How many cards the counter says we should have and did not get.

    Zero when the counter is absent — an unknown gap is not a gap of zero,
    but it is not a number this function may invent either. The engines log
    the counter alongside, so an absent one is visible rather than silent.
    """
    expected = results_range(html).expected_on_page
    if expected is None:
        return 0
    return max(0, expected - parsed)


# ---------------------------------------------------------------------------
# Detection: what kind of answer did we get?
# ---------------------------------------------------------------------------
# Expedia's refusal is a single handler that can serve any of five different
# vendors, and it says which one in its own page config rather than in its
# markup:
#
#     "whichChallenge": "datadome-challenge"
#     "siteKey": <reCAPTCHA v2>, "recaptchaV3Key": …, "turnstileSiteKey": …,
#     "arkoseClientApiUrl": …, "powComplexity": 20
#
# So the vendor is DATA, and a list of vendor markers cannot name it: the
# same URL can come back DataDome this time and reCAPTCHA the next. What is
# constant is the handler — HTTP 429, `<title>Bot or Not?</title>`, and the
# app naming itself `captcha-pwa` / `wildcard-challenge-handler`.
CHALLENGE_MARKERS: Tuple[str, ...] = (
    "wildcard-challenge-handler",
    "captcha-pwa",
    '"botOrNot"',
    "botOrNot",
    "Bot or Not?",
    "DATADOME-CHALLENGE",
)

# Which vendors this repo can actually pay to have solved. `captcha_solver`
# implements reCAPTCHA v2 and v3 and nothing else — no DataDome, no Arkose,
# no proof-of-work — so a challenge naming one of those is reported as
# `blocked` rather than `challenge`, and no solve is attempted and nothing is
# charged (§8's "detected ≠ blocking ≠ paying"). The measured challenge on
# this site was `datadome-challenge`, so this distinction is the normal case
# here rather than an edge one.
SOLVABLE_CHALLENGES: Tuple[str, ...] = ("recaptcha",)

_WHICH_CHALLENGE_RE = re.compile(r'whichChallenge\\*"\s*:\s*\\*"([a-z0-9_-]+)',
                                 re.I)

# Deliberately NOT a marker set of its own: every string a vendor list would
# hold — `akamai` above all — is on the pages this site SERVES. Vrbo is
# fronted by Akamai Bot Manager and every good page loads its sensor script
# from a rotating path on vrbo.com's own origin, so `akamai` matches a 899 KB
# page holding the full grid. §18's rule, and the reason it is a rule: a
# marker that matches every page is worse than no marker.
#
# The generic reCAPTCHA/hCaptcha shapes below are kept as a forward-looking
# detector only, for the case where the site renders one INSIDE a page rather
# than through the handler above.
BOT_CHALLENGE_MARKERS: Tuple[str, ...] = (
    "recaptcha/api2/anchor",
    "recaptcha/api2/bframe",
    "recaptcha/api.js",
    "hcaptcha.com/captcha",
    "challenges.cloudflare.com",
)

# Positive detection: what every page this site serves is built out of, and
# what no interstitial and no browser error page is. Vrbo's own asset and
# media hosts. This is the signal that answers correctly for Chromium's own
# network-error page, which carries `<title>www.vrbo.com</title>` and would
# pass a title check (§18).
_ASSET_MARKER = re.compile(
    r"c\.travel-assets\.com|a\.travel-assets\.com|media\.vrbo\.com"
    r"|b\.travel-assets\.com", re.I)
# Three, not one, and the threshold is checked AFTER the unambiguous positive
# signals below rather than before them (§17's classification-order trap): a
# leanly-built real page must not come back `blocked`. Measured: 40+ matches
# on every served page, and — the case that matters — the challenge page
# scores 12, because it is built out of travel-assets too. So this threshold
# alone cannot tell a block from a page, and it is never asked to.
_ASSET_MIN_MATCHES = 3


def served_by_vrbo(html: str) -> bool:
    """Whether this page was built out of the site's own assets."""
    return len(_ASSET_MARKER.findall(html or "")) >= _ASSET_MIN_MATCHES


def challenge_vendor(html: str) -> Optional[str]:
    """Which vendor the challenge handler picked this time, or None."""
    match = _WHICH_CHALLENGE_RE.search(html or "")
    return match.group(1).lower() if match else None


def is_challenge_page(html: str) -> bool:
    """Whether this is Expedia's Bot-or-Not handler rather than a page."""
    text = html or ""
    return any(marker in text for marker in CHALLENGE_MARKERS)


def detect_block_marker(html: str) -> Optional[str]:
    """The refusal marker this page carries, or None.

    Names the VENDOR where the handler stated one, because "blocked
    (datadome-challenge)" tells a reader what to do next and "blocked" does
    not.
    """
    if not is_challenge_page(html):
        return None
    return challenge_vendor(html) or "wildcard-challenge-handler"


def detect_bot_challenge(html: str, url: str = "") -> Optional[str]:
    """The SOLVABLE challenge this page rendered, or None.

    Silent about a challenge this repo cannot solve, so no solve is attempted
    and nothing is charged for it.
    """
    text = html or ""
    if is_challenge_page(text):
        vendor = challenge_vendor(text) or ""
        if any(known in vendor for known in SOLVABLE_CHALLENGES):
            return vendor
        return None
    lowered = text.lower()
    for marker in BOT_CHALLENGE_MARKERS:
        if marker.lower() in lowered:
            return marker
    return None


# The site's own "nothing matched" copy, COPIED FROM ITS OWN PAGES rather
# than translated — all three of these were read off a real capture of a
# search for a destination the site cannot resolve:
#
#   en  "We don't have any properties that match your search criteria"
#   de  "Wir haben keine Unterkünfte, die deinen Suchkriterien entsprechen"
#   fr  "Nous n'avons aucun hébergement qui correspond à vos critères"
#
# The first guess at this list was invented ("No properties found", "0
# properties") and matched none of them. A marker set written from
# imagination is how an empty page comes back as `shell` and spends a
# 25-second readiness wait on an answer the site had already given (§18).
#
# Matched on a fragment rather than the whole sentence so a copy tweak does
# not silently break the state, and apostrophes are normalised because the
# French page uses U+2019 where a source file would type U+0027.
#
# NOT MEASURED: bookabach.co.nz and stayz.com.au both serve English, so the
# `en` fragment is expected to cover them; that has not been verified and
# the failure mode if it does not is a 25-second wait and exit 4, which is
# the right answer arrived at slowly.
NO_RESULTS_MARKERS: Tuple[str, ...] = (
    "any properties that match your search criteria",
    "keine unterkünfte, die deinen suchkriterien entsprechen",
    "aucun hebergement qui correspond a vos criteres",
    "aucun hébergement qui correspond à vos critères",
)

_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'"})


def is_no_results(html: str) -> bool:
    """Whether the site said, in its own words, that nothing matched.

    Deliberately a POSITIVE signal only. A search page with an empty grid and
    none of this copy is a page still painting, not an empty result — and the
    two want opposite responses (§18). Getting that backwards would report a
    slow page as an empty catalogue.
    """
    lowered = (html or "").translate(_APOSTROPHES).lower()
    return any(marker in lowered for marker in NO_RESULTS_MARKERS)


def detect_page_state(html: Optional[str], status: Optional[int] = None,
                      url: str = "") -> str:
    """Which of five states this response is.

    Ordered by how much each signal PROVES rather than by what is cheap to
    check (§17). The unambiguous positive — cards in the document — comes
    first, so a page the site plainly served can never be reported as
    blocked; the challenge handler is checked before the status code because
    it says WHICH vendor, which the status does not; and the asset threshold
    is last, because it is the only signal here that is a threshold.

    `status` is positional and second, matching `page_flow.classify`. Two
    engines in a sibling repo called this with `status` as a keyword and both
    crashed on their first fetch, invisibly to every offline check (§17).
    """
    if html is None:
        return "blocked"

    soup = BeautifulSoup(html, "html.parser")

    # 1. Unambiguous positive: the grid is in the document.
    if soup.select(SELECTORS["item_card"]):
        return "content"

    # 1b. The same question for the other page kind. A property page has no
    #     cards on it by design, so without this it falls through to `shell`
    #     and an engine waits twenty seconds for a grid that is never coming
    #     (§17: the auctions-index hole, in this repo's shape). The anchor is
    #     the page's own title plus one of the blocks only a property page
    #     has, so a challenge page carrying an `h1` cannot pass.
    if listing_kind(url) == "property" and soup.select_one("h1") is not None \
            and (soup.select_one(SELECTORS["detail_price"]) is not None
                 or soup.select_one(SELECTORS["detail_reviews"]) is not None
                 or soup.select_one(SELECTORS["detail_address"]) is not None):
        return "content"

    # 2. Its own empty answer, printed in its own language.
    if is_no_results(html):
        return "empty"

    # 3. The challenge handler, which names its vendor. Before the status
    #    check because it is the more informative of the two, and before the
    #    asset threshold because the handler is itself built out of the
    #    site's assets and would otherwise read as "served".
    if is_challenge_page(html):
        return "challenge" if detect_bot_challenge(html) else "blocked"

    # 4. A refusal with no page to read. 429 is the one this site actually
    #    uses; the others are here because a CDN in front of it may not.
    if status is not None and (status in (401, 403, 429) or status >= 500):
        return "blocked"

    # 5. A generic in-page challenge, if one is ever rendered.
    if detect_bot_challenge(html):
        return "challenge"

    # 6. Not built out of this site's assets at all — a browser error page or
    #    somebody else's interstitial.
    if not served_by_vrbo(html):
        return "blocked"

    # 7. Served, ours, and the grid is not there yet. A search page's first
    #    response is a shell: the grid arrives over client-side GraphQL, so
    #    this state wants the readiness wait and the scroll, NOT a refetch —
    #    refetching a shell buys another shell (§18).
    return "shell"


# ---------------------------------------------------------------------------
# Listing rows
# ---------------------------------------------------------------------------
_DASH = "-‐‑‒–—―"
_DATE_RANGE_RE = re.compile(r"\d.*[" + _DASH + r"].*\d")
_RATING_SCALE_RE = re.compile(r"^\s*([\d.,]+)\s*\D{1,20}?\s*(\d{1,3})\s*$")
_PARENTHESISED_RE = re.compile(r"^\(.*\d.*\)$", re.S)


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node is not None else ""


def _a11y_texts(card) -> List[str]:
    """The card's screen-reader-only strings, in document order.

    Load-bearing rather than a curiosity: the rating's scale, the review
    count and the full price sentence are each published here and nowhere
    else in a machine-readable form.
    """
    return [_text(n) for n in card.select(SELECTORS["a11y"])]


def _rating(card) -> Tuple[Optional[float], Optional[float]]:
    """(rating, scale) from the card's badge, or (None, None).

    The badge class is shared with the "Premier Host" ribbon — measured, 3 of
    50 cards carry both — so the badge is chosen by whether its text is a
    NUMBER rather than by its position. The scale comes from the
    screen-reader string beside it (`8.6 out of 10`, `8,0 von 10`), and is
    returned rather than assumed: this site rates out of TEN where most of
    this family's sites rate out of five, and a consumer comparing the two
    columns without the scale would be comparing nothing.
    """
    value: Optional[float] = None
    for badge in card.select(SELECTORS["card_badge"]):
        parsed = numbers_in(_text(badge))
        if parsed and _text(badge).strip("0123456789.,  ") == "":
            value = parsed[0]
            break
    if value is None:
        return None, None
    scale: Optional[float] = None
    for text in _a11y_texts(card):
        match = _RATING_SCALE_RE.match(text)
        if match:
            scored = _normalize_amount(match.group(1))
            if scored is not None and abs(scored - value) < 0.051:
                scale = float(match.group(2))
                break
    return value, scale


def _review_count(card) -> Optional[int]:
    """The review count, from the parenthesised screen-reader string.

    `(1,299 reviews)` and `(1.614 bewertungen)` — one of those is 1299 and
    the other 1614, and a naive `\\d+` returns 1 for the second.
    """
    for text in _a11y_texts(card):
        if _PARENTHESISED_RE.match(text):
            found = int_in(text)
            if found is not None:
                return found
    return None


def _price_block(card) -> Tuple[Optional[float], Optional[str], List[str]]:
    """(amount, price_source, the block's other lines verbatim).

    Two sources, in descending trust:

      "card"       the rendered amount node — what the visitor sees
      "card-a11y"  the screen-reader sentence ("The current price is $81",
                   "Der aktuelle Preis beträgt 66 €.")

    Recorded in `price_source` because the two can disagree while a run
    otherwise looks healthy, and a diff between two runs that differ only in
    which node was readable should say `source_changed`, not `changed` (§8).
    """
    block = card.select_one(SELECTORS["card_price"])
    if block is None:
        return None, None, []
    lines = [_text(n) for n in block.select("div, span")]
    lines = [line for line in lines if line]

    rendered = block.select_one(".uitk-type-500")
    amount = price_in(_text(rendered)) if rendered is not None else None
    source = "card" if amount is not None else None
    if amount is None:
        for text in lines:
            amount = price_in(text)
            if amount is not None:
                source = "card-a11y"
                break
    # Deduplicate while keeping order: the block nests, so the same sentence
    # comes back once per ancestor.
    seen: List[str] = []
    for line in lines:
        if line not in seen:
            seen.append(line)
    return amount, source, seen


def _stay_dates(lines: List[str]) -> Optional[str]:
    """The stay the price is quoted for, verbatim, or None.

    THE trap on this site, and it is not a parsing one. With no dates in the
    URL, the site quotes every property its own cheapest one-night stay:
    three cards in one load read `Sep 28 - Sep 29`, `Sep 16 - Sep 17` and
    `Sep 24 - Sep 25`. Those prices are not comparable to each other, and the
    same row will appear to change price between two runs when all that moved
    was the date. So the dates ride along in their own column, and the README
    says to pin `startDate`/`endDate` for price monitoring.

    Recognised structurally — two numbers either side of a dash — rather than
    by a month name, because the German storefront writes `30. Sept.–1. Okt.`
    with an en dash and no currency in sight.
    """
    for line in lines:
        if _DATE_RANGE_RE.search(line) and not prices_in(line):
            return line
    return None


def _property_summary(card) -> Tuple[Optional[str], Optional[str],
                                     Optional[int], Optional[int]]:
    """(verbatim summary, property type, bedrooms, beds).

    The line reads `Aparthotel ·\\xa01\\xa0bedroom ·\\xa02 beds`, and on a
    hotel card in Germany it reads `Aparthotel` and nothing else. The type is
    the first `·` segment and is a real value in every locale. Bedrooms and
    beds are read POSITIONALLY — second segment, third segment — because the
    words around them are per-locale and a lexical match would leave both
    columns null on every non-English storefront.

    That is an assumption about ordering, so it is made narrowly (a segment
    must hold exactly one integer) and the verbatim line is kept beside the
    split columns, which is what makes a wrong split visible instead of
    silent.
    """
    node = card.select_one(SELECTORS["card_summary"])
    if node is None:
        return None, None, None, None
    raw = _text(node)
    if not raw:
        return None, None, None, None
    parts = [p.strip() for p in raw.split("·")]
    ptype = parts[0] or None

    def _single_int(text: str) -> Optional[int]:
        found = numbers_in(text)
        return int(round(found[0])) if len(found) == 1 else None

    bedrooms = _single_int(parts[1]) if len(parts) > 1 else None
    beds = _single_int(parts[2]) if len(parts) > 2 else None
    return raw, ptype, bedrooms, beds


def _badges(card) -> List[str]:
    """Non-numeric badges — "Premier Host" and whatever joins it."""
    out: List[str] = []
    for badge in card.select(SELECTORS["card_badge"]):
        text = _text(badge)
        if text and not (text.strip("0123456789.,  ") == ""):
            out.append(text)
    return out


def _amenities(card) -> List[str]:
    """The card's amenity highlights.

    Present on 18 of 18 cards in one capture and 0 of 50 in another of the
    same URL minutes later, so this is an A/B variant of the card rather than
    a parsing failure — worth a column because when it is there it is there
    for every row, and worth this comment so an empty column is not read as a
    bug.
    """
    node = card.select_one(SELECTORS["card_amenities"])
    if node is None:
        return []
    units = [_text(u) for u in node.select("[data-amenity-unit]")]
    return [u for u in units if u]


def _image_url(card) -> Optional[str]:
    """A real image URL, or None.

    Sparse on purpose. 41 of 50 cards carry no `<img>` element at all — the
    gallery is not mounted below the fold — so this column is populated for
    the cards that were on screen and null for the rest. Recognised
    POSITIVELY by the media host, so a future placeholder cannot fill the
    column with something that is not an image (§4).
    """
    for img in card.select(SELECTORS["card_image"]):
        src = (img.get("src") or "").strip()
        if src.startswith("https://media."):
            return src
    return None


def _absolute(url: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")) + href


def parse_products(html: str, url: str, page: int = 1,
                   page_currency_code: Optional[str] = None) -> List[Product]:
    """Every property card on this listing page, in the site's own order.

    `page` is threaded in rather than defaulted, because `position` restarts
    at 1 on every page and a `page` column stuck at 1 makes the pair
    worthless — 60 of 119 rows of a sibling repo's two-page run silently
    claimed a position another row already held (§18).
    """
    soup = BeautifulSoup(html or "", "html.parser")
    currency = page_currency_code or page_currency(html or "")
    source = source_of(url)
    rows: List[Product] = []

    for index, card in enumerate(soup.select(SELECTORS["item_card"]), start=1):
        link = card.select_one('a[data-stid="open-product-information"]') \
            or card.select_one("a.uitk-card-link") \
            or card.select_one("a[href]")
        href = (link.get("href") if link is not None else "") or ""
        absolute = _absolute(url, href)
        # Tracking stripped before the URL is written down: a card's href
        # carries a per-load `searchId` and a millisecond `pwa_ts`, so the
        # same property's URL would differ on every run and every diff would
        # report it as changed.
        clean = strip_tracking(absolute) if absolute else ""

        amount, price_source, lines = _price_block(card)
        rating, scale = _rating(card)
        summary, ptype, bedrooms, beds = _property_summary(card)

        title_node = card.select_one(SELECTORS["card_title"])
        if title_node is None:
            for candidate in card.select("h3"):
                if "is-visually-hidden" not in (candidate.get("class") or []):
                    title_node = candidate
                    break

        rows.append(Product(
            source=source,
            url=clean or absolute,
            sku=sku_from_url(clean or absolute),
            title=_text(title_node) or None,
            price=amount,
            # The page's own ISO code where it states one, never a symbol
            # read off the tile, and null rather than a defaulted "USD" when
            # neither is available (§4).
            currency=currency or (currency_in(" ".join(lines), url) if lines else None),
            rating=rating,
            rating_scale=scale,
            review_count=_review_count(card),
            image_url=_image_url(card),
            category=None,
            price_source=price_source,
            page=page,
            position=index,
            expedia_property_id=expedia_property_id_from_url(absolute),
            property_type=ptype,
            property_summary=summary,
            bedrooms=bedrooms,
            beds=beds,
            location_note=_text(card.select_one(
                SELECTORS["card_location_note"])) or None,
            badges=_badges(card),
            amenities=_amenities(card),
            stay_dates=_stay_dates(lines),
            price_note=" | ".join(
                line for line in lines
                if line and not prices_in(line) and line != _stay_dates(lines)
            ) or None,
            listing_kind="search",
        ))
    return rows


# ---------------------------------------------------------------------------
# A property page
# ---------------------------------------------------------------------------
def breadcrumb_category(html: str) -> Optional[str]:
    """The location path from the page's own BreadcrumbList JSON-LD.

    The only thing either of a detail page's two JSON-LD blocks is good for:
    they are a `BreadcrumbList` and an `FAQPage`, and neither is a `Product`
    — so there is no structured price or currency on this site anywhere, on
    any page kind. Handles the `itemListElement` shapes §4 lists, including
    a final crumb with no `item` (the property itself, which is not a link).
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict) or node.get("@type") != "BreadcrumbList":
                continue
            items = node.get("itemListElement")
            if not isinstance(items, list):
                continue
            names = [i.get("name") for i in items
                     if isinstance(i, dict) and i.get("name")]
            # The last crumb is the property's own title, not a category.
            return " / ".join(names[:-1]) if len(names) > 1 else None
    return None


def _meta(soup, key: str) -> Optional[str]:
    node = soup.find("meta", attrs={"property": key}) or \
        soup.find("meta", attrs={"name": key})
    return (node.get("content") or "").strip() or None if node else None


def parse_property_page(html: str, url: str) -> List[Product]:
    """One row for the property this detail page describes.

    Returns a list so an engine's merge path is the same shape in both modes.
    A detail page adds the street address and the location summary, which a
    card does not publish at all; it does NOT add a structured price, because
    there is none on this site.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    currency = page_currency(html or "")

    block = soup.select_one(SELECTORS["detail_price"])
    amount, price_source, lines = (None, None, [])
    if block is not None:
        lines = [t for t in (_text(n) for n in block.select("div, span")) if t]
        rendered = block.select_one(".uitk-type-500")
        amount = price_in(_text(rendered)) if rendered is not None else None
        price_source = "detail" if amount is not None else None
        if amount is None:
            for text in lines:
                amount = price_in(text)
                if amount is not None:
                    price_source = "detail-a11y"
                    break

    review_node = soup.select_one(SELECTORS["detail_reviews"])
    rating = scale = None
    review_count = None
    if review_node is not None:
        rating, scale = _rating(review_node)
        review_count = int_in(_text(soup.select_one('[data-stid="reviews-link"]')
                                    or review_node))

    seen: List[str] = []
    for line in lines:
        if line not in seen:
            seen.append(line)

    return [Product(
        source=source_of(url),
        url=strip_tracking(url),
        sku=sku_from_url(url),
        title=_text(soup.select_one(SELECTORS["detail_title"])) or _meta(soup, "og:title"),
        price=amount,
        currency=currency or (currency_in(" ".join(seen), url) if seen else None),
        rating=rating,
        rating_scale=scale,
        review_count=review_count,
        image_url=_meta(soup, "og:image"),
        category=breadcrumb_category(html),
        price_source=price_source,
        page=None,
        position=None,
        expedia_property_id=expedia_property_id_from_url(url),
        location_note=_text(soup.select_one(SELECTORS["detail_address"])) or None,
        stay_dates=_stay_dates(seen),
        price_note=" | ".join(
            line for line in seen
            if not prices_in(line) and line != _stay_dates(seen)) or None,
        listing_kind="property",
    )]
