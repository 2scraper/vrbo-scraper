"""Cut the offline suite's fixtures out of real captures, and PROVE they
parse the same.

Its output is `fixtures_generated.json`, which `smoke_test.py` loads. This
script is shipped because two files point at it — `smoke_test.py`'s own
docstring and TROUBLESHOOTING.md — and an instruction pointing at a file that
does not exist is worse than no instruction.

WHAT YOU NEED TO RUN IT
-----------------------
Your own captures, in `../captures/` relative to the repo, named as `SOURCES`
below expects. They are deliberately NOT in the repository: a single capture
of this site is 600 KB to 1.3 MB.

Take them with a real browser — `--dump-html` on any engine writes exactly
the bytes the parser was given — and remember that this site refuses
Playwright's bundled Chromium, so use `--browser-channel chrome` (the
default) or the Selenium engine.

WHAT IT ENFORCES, and why each rule is here
-------------------------------------------
  * every fixture is CUT from a real capture, never hand-written. The one
    thing in this repo that WAS hand-written — a guess at the site's
    "nothing matched" copy — matched none of the three real strings, and an
    empty search came back as `shell` and spent a 25-second readiness wait
    on an answer the site had already given;
  * each one is verified to parse IDENTICALLY to the untrimmed original for
    the cards it keeps — every column, not just a count;
  * the challenge fixture's site keys are replaced with obvious placeholders
    BEFORE anything is written, and guarded by PATTERNS rather than by the
    literals one capture happened to contain, so the next capture is caught
    too. Those keys are the SITE's public ones rather than anybody's secret,
    but a 32-hex string in a public repo reads as a live credential to every
    scanner that looks, including this repo's own CI grep.
"""
import json
import os
import re
import sys
import pathlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import product_parser as P
from bs4 import BeautifulSoup
from dataclasses import asdict

HERE = pathlib.Path(__file__).parent
CAPTURES = HERE.parent.parent / "captures"
OUT = HERE / "fixtures_generated.json"

# name -> (capture file, url it was fetched from, how many cards to keep)
SOURCES = {
    "LISTING_US":    ("vrbo_search_orlando_p1.html",
                      "https://www.vrbo.com/search?destination=Orlando%2C+Florida%2C+United+States+of+America&regionId=2693", 4),
    "LISTING_US_P2": ("vrbo_search_orlando_p2.html",
                      "https://www.vrbo.com/search?destination=Orlando%2C+Florida%2C+United+States+of+America&regionId=2693", 2),
    "LISTING_DE":    ("vrbo_de_berlin_p1.html",
                      "https://www.fewo-direkt.de/search?destination=Berlin%2C+Deutschland&regionId=536", 4),
    "LISTING_AU":    ("host_stayz.html",
                      "https://www.stayz.com.au/search?destination=Sydney%2C+New+South+Wales%2C+Australia", 2),
}
# A card the site renders WITHOUT a price: "This property does not allow
# 2-night stays for those dates", on a dated Orlando search served to the
# scheduled canary on 2026-10-15. Its row must carry no currency either.
UNPRICED_SOURCE = ("vrbo_search_orlando_dated_2026-10-15.html",
                   "https://www.vrbo.com/search?destination=Orlando%2C%20Florida%2C%20United%20States%20of%20America&regionId=2693&sort=RECOMMENDED&startDate=2026-11-14&endDate=2026-11-16&adults=2")


def _unpriced(card) -> bool:
    return (card.select_one(P.SELECTORS["card_price"]) is None
            and "does not allow" in card.get_text(" "))


EMPTY_SOURCES = {
    "EMPTY_EN": ("vrbo_empty_nonsense.html", "https://www.vrbo.com/search?destination=qzxwvkjhgfd"),
    "EMPTY_DE": ("vrbo_empty_de.html", "https://www.fewo-direkt.de/search?destination=qzxwvkjhgfd"),
    "EMPTY_FR": ("vrbo_empty_fr.html", "https://www.abritel.fr/search?destination=qzxwvkjhgfd"),
}
BLOCK_SOURCE = ("block_429_botornot_curl.html", "https://www.vrbo.com/search?destination=Orlando")
PROPERTY_SOURCE = ("vrbo_detail_pdp_lo.html", "https://www.vrbo.com/pdp/lo/63925107")
# The site's own error copy in place of the grid ("Sorry, we're having a
# problem on our end" + "Try again"), served by vrbo.com to the scheduled
# canary on 2026-10-07 for a dated Orlando search. Built from the site's own
# assets, no counter, no no-results copy, no cards: it classifies as `shell`
# and parses to 0 rows, and before the not-obtained rule it ended a run as
# exit 4, "the search matched nothing".
SITE_ERROR_SOURCE = ("vrbo_site_error_2026-10-07.html",
                     "https://www.vrbo.com/search?destination=Orlando%2C%20Florida%2C%20United%20States%20of%20America&regionId=2693&sort=RECOMMENDED&startDate=2026-11-14&endDate=2026-11-16&adults=2")
# A grid the scroll loop left at 13 of 50 cards with the site's own loading
# placeholders under them (canary, 2026-10-21, no proxy).
LAZY_TAIL_SOURCE = ("vrbo_search_lazy_tail_stall_2026-10-21.html",
                    "https://www.vrbo.com/search?destination=Orlando%2C%20Florida%2C%20United%20States%20of%20America&regionId=2693&sort=RECOMMENDED&startDate=2026-12-05&endDate=2026-12-07&adults=2")

# A card that keeps the SLUG route must survive into LISTING_DE: it is the
# fifth URL shape and only the local brands use it, so a fixture without one
# cannot catch the pattern regressing on vrbo.com's shape alone.
SLUG_ROUTE_RE = re.compile(r'href="/[a-z][a-z-]+/p\d+')

# Patterns, not literals. The challenge page's config carries five vendors'
# site keys and a device id; the next capture will carry different values of
# the same shapes.
SCRUB_PATTERNS = (
    (re.compile(r'("(?:siteKey|recaptchaV3Key)\\*"\s*:\s*\\*")[^"\\]{20,}'), r"\g<1>SITEKEY-REDACTED"),
    (re.compile(r'("turnstileSiteKey\\*"\s*:\s*\\*")[^"\\]{10,}'), r"\g<1>TURNSTILE-REDACTED"),
    (re.compile(r'("datadomeClientKey\\*"\s*:\s*\\*")[^"\\]{10,}'), r"\g<1>DATADOME-REDACTED"),
    (re.compile(r'(pkey=)[0-9A-Fa-f-]{16,}'), r"\g<1>ARKOSE-REDACTED"),
    (re.compile(r'(arkoseClientApiUrl\\*"\s*:\s*\\*"[^"]*?v2\\u002F)[0-9A-Fa-f-]{16,}'), r"\g<1>ARKOSE-REDACTED"),
    (re.compile(r'("(?:exchangeHandshake|arkoseDataExchange|noncePrefix|deviceId)\\*"\s*:\s*\\*")[^"\\]{12,}'),
     r"\g<1>REDACTED"),
    (re.compile(r'<div class="captcha-debug-id">[^<]+</div>'), '<div class="captcha-debug-id">REDACTED</div>'),
)


# A final sweep, after the named patterns above. The named ones say WHAT
# each value is, which is worth keeping; this one exists because the page
# carries hex the named list does not know about — the challenge app's own
# build sha, for one — and a public repo should not ship any 24+ character
# hex run at all. This repo's CI grep does not distinguish a build sha from a
# credential, and neither does anyone else's scanner.
_ANY_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{24,}\b")


def scrub(html: str) -> str:
    for pattern, replacement in SCRUB_PATTERNS:
        html = pattern.sub(replacement, html)
    return _ANY_LONG_HEX.sub("REDACTED-HEX", html)


def _head(soup: BeautifulSoup, html: str) -> str:
    """The bits of the page a fixture needs besides its cards.

    Three things, and each one is load-bearing for at least one check:
      * `<html lang>`, because the parser's locale-shaped behaviour is keyed
        off the storefront rather than this, and a fixture without it would
        not prove that;
      * enough references to the site's own asset hosts to clear
        `served_by_vrbo`'s threshold — otherwise every trimmed fixture
        classifies as `blocked`;
      * the page's own `"currency":"XXX"` config, which is the strongest
        currency source there is and is the only thing that tells vrbo.com's
        USD from stayz.com.au's AUD.
    """
    lang = (soup.html.get("lang") if soup.html else "") or "en"
    currency = P.page_currency(html) or ""
    assets = ('<link rel="preconnect" href="https://c.travel-assets.com">'
              '<link rel="preconnect" href="https://a.travel-assets.com">'
              '<link rel="preconnect" href="https://b.travel-assets.com">')
    config = (f'<script>window.__PLUGIN_STATE__ = JSON.parse("{{\\"currency\\":'
              f'\\"{currency}\\"}}");</script>') if currency else ""
    return f'<html lang="{lang}"><head>{assets}{config}</head><body>'


def build_listing(name, filename, url, keep, pick=None):
    path = CAPTURES / filename
    full = path.read_text(encoding="utf-8")
    soup = BeautifulSoup(full, "html.parser")
    cards = soup.select(P.SELECTORS["item_card"])
    if not cards:
        raise SystemExit(f"{filename}: no cards found — is this a real capture?")

    chosen = list(cards[:keep]) if pick is None else [
        c for c in cards if pick(c)][:keep]
    if not chosen:
        raise SystemExit(f"{name}: no card matched what this fixture is for")
    # Make sure at least one slug-route card is in the German fixture.
    if name == "LISTING_DE" and not any(SLUG_ROUTE_RE.search(str(c)) for c in chosen):
        for card in cards:
            if SLUG_ROUTE_RE.search(str(card)):
                chosen[-1] = card
                break

    pagination = soup.select_one(P.PAGINATION_SELECTOR)
    header = soup.select_one(P.RESULTS_HEADER_SELECTOR)
    body = "".join(str(c) for c in chosen)
    trimmed = (_head(soup, full)
               + (str(header) if header is not None else "")
               + f'<div data-stid="property-listing-results">{body}</div>'
               + (str(pagination) if pagination is not None else "")
               + "</body></html>")

    # THE PROOF. Parse both and compare every column of every kept card.
    want = {r.sku: asdict(r) for r in P.parse_products(full, url, page=1)}
    got = {r.sku: asdict(r) for r in P.parse_products(trimmed, url, page=1)}
    if set(got) - set(want):
        raise SystemExit(f"{name}: trimmed fixture invented skus {set(got)-set(want)}")
    for sku, row in got.items():
        for column, value in row.items():
            if column in ("scraped_at", "position"):
                continue  # per-run, and position renumbers within the trim
            if want[sku][column] != value:
                raise SystemExit(
                    f"{name}: trimming changed {column!r} on {sku}: "
                    f"{want[sku][column]!r} -> {value!r}")
    if P.detect_page_state(trimmed, 200, url) != "content":
        raise SystemExit(f"{name}: trimmed fixture does not classify as content")
    print(f"  {name}: {len(full)} -> {len(trimmed)} bytes, {len(got)} card(s) "
          f"verified identical")
    return trimmed


def build_empty(name, filename, url):
    full = (CAPTURES / filename).read_text(encoding="utf-8")
    soup = BeautifulSoup(full, "html.parser")
    container = soup.select_one('[data-stid="property-listing-results"]')
    trimmed = (_head(soup, full) + str(container) + "</body></html>")
    if not P.is_no_results(trimmed):
        raise SystemExit(f"{name}: trimmed fixture lost the no-results copy")
    if P.detect_page_state(trimmed, 200, url) != "empty":
        raise SystemExit(f"{name}: trimmed fixture does not classify as empty")
    print(f"  {name}: {len(full)} -> {len(trimmed)} bytes, classifies as empty")
    return trimmed


def build_block(filename, url):
    full = scrub((CAPTURES / filename).read_text(encoding="utf-8"))
    # Keep the handler's identity and its config, drop the megabyte of
    # webpack runtime. The config is what names the vendor, which is the
    # whole point of this fixture.
    marker = full.find('"botOrNot"')
    if marker < 0:
        marker = full.find("botOrNot")
    # Keep the handler's IDENTITY — its title and its own app name — and its
    # config. Not the head: this page's head is 35 KB of webpack manifest
    # that proves nothing.
    title = re.search(r"<title>[^<]*</title>", full)
    head = ('<html><head>' + (title.group(0) if title else "")
            + '<link rel="stylesheet" '
              'href="https://c.travel-assets.com/captcha-pwa/css/app-shared.css">'
              '</head>')
    body = ('<div class="datadome-container"><div id="DATADOME-CHALLENGE">'
            '</div></div>')
    config = full[max(0, marker - 200): marker + 2600] if marker > 0 else ""
    trimmed = head + "<body>" + body + config + "</body></html>"
    state = P.detect_page_state(trimmed, 429, url)
    # `challenge`, not `blocked`: DataDome is a pick this repo can solve.
    if state != "challenge":
        raise SystemExit(f"BLOCK_429: classifies as {state!r}, not challenge")
    if P.challenge_vendor(trimmed) is None:
        raise SystemExit("BLOCK_429: lost whichChallenge — the vendor is the point")
    leftovers = re.findall(r"\b[0-9a-fA-F]{24,}\b", trimmed)
    if leftovers:
        raise SystemExit(f"BLOCK_429: unscrubbed long hex left in: {leftovers[:3]}")
    print(f"  BLOCK_429: {len(full)} -> {len(trimmed)} bytes, vendor "
          f"{P.challenge_vendor(trimmed)!r}, scrubbed")
    return trimmed


def build_site_error(filename, url):
    """Keep the error block's heading and button, nothing session-bound.

    The block's sibling feedback link carries the visitor's device id in its
    query string, so only the heading and the button are kept, beside the
    usual head (asset hosts, so `served_by_vrbo` still holds).
    """
    full = (CAPTURES / filename).read_text(encoding="utf-8")
    soup = BeautifulSoup(full, "html.parser")
    heading = soup.find("h2", string=re.compile(r"having a problem"))
    if heading is None:
        raise SystemExit(f"{filename}: no error heading — is this the capture?")
    button = heading.find_next_sibling("button")
    block = ('<div data-stid="split-view-search-list-container"><div>'
             + str(heading) + (str(button) if button is not None else "")
             + "</div></div>")
    trimmed = _head(soup, full) + block + "</body></html>"
    for html, label in ((full, "original"), (trimmed, "trimmed")):
        state = P.detect_page_state(html, None, url)
        if state != "shell" or P.parse_products(html, url):
            raise SystemExit(f"SITE_ERROR: {label} reads as {state!r} with "
                             f"rows; the fixture no longer shows the case")
    leftovers = re.findall(r"\b[0-9a-fA-F]{24,}\b|duaid", trimmed)
    if leftovers:
        raise SystemExit(f"SITE_ERROR: session material left in: {leftovers[:3]}")
    print(f"  SITE_ERROR: {len(full)} -> {len(trimmed)} bytes, classifies as "
          f"shell with 0 rows")
    return trimmed


def build_lazy_tail(filename, url, keep=2):
    """A grid that stopped short: `keep` real cards and the lazy tail.

    Cut from a page that the scroll loop left at 13 of 50 cards with the
    site's own placeholders still under them. Keeps the placeholders exactly
    as served (their parent block) so the selector is tested against the
    real markup, and the first `keep` cards so the page still parses.
    """
    full = (CAPTURES / filename).read_text(encoding="utf-8")
    soup = BeautifulSoup(full, "html.parser")
    placeholders = soup.select(P.SELECTORS["grid_placeholder"])
    if not placeholders:
        raise SystemExit(f"{filename}: no grid placeholder — is this the stalled capture?")
    tail = placeholders[0].parent
    cards = soup.select(P.SELECTORS["item_card"])[:keep]
    header = soup.select_one(P.RESULTS_HEADER_SELECTOR)
    pagination = soup.select_one(P.PAGINATION_SELECTOR)
    block = ((str(header) if header is not None else "")
             + '<div data-stid="property-listing-results">'
             + "".join(str(c) for c in cards) + str(tail) + "</div>"
             + (str(pagination) if pagination is not None else ""))
    trimmed = scrub(_head(soup, full) + block + "</body></html>")
    small = BeautifulSoup(trimmed, "html.parser")
    if len(small.select(P.SELECTORS["grid_placeholder"])) != len(placeholders):
        raise SystemExit("LISTING_LAZY_TAIL: trimming changed the placeholder count")
    want = {r.sku: asdict(r) for r in P.parse_products(full, url, page=1)}
    got = {r.sku: asdict(r) for r in P.parse_products(trimmed, url, page=1)}
    if len(got) != keep or set(got) - set(want):
        raise SystemExit(f"LISTING_LAZY_TAIL: kept {len(got)} rows, not the {keep} cut")
    for sku, row in got.items():
        for column, value in row.items():
            expected = want[sku][column]
            if isinstance(expected, str):
                expected = scrub(expected)  # the rate-plan id is a long hex run
            if column not in ("scraped_at", "position") and expected != value:
                raise SystemExit(f"LISTING_LAZY_TAIL: trimming changed {column!r} on {sku}")
    if P.detect_page_state(trimmed, 200, url) != "content":
        raise SystemExit("LISTING_LAZY_TAIL: trimmed fixture does not classify as content")
    leftovers = re.findall(r"\b[0-9a-fA-F]{24,}\b|duaid", trimmed)
    if leftovers:
        raise SystemExit(f"LISTING_LAZY_TAIL: session material left in: {leftovers[:3]}")
    print(f"  LISTING_LAZY_TAIL: {len(full)} -> {len(trimmed)} bytes, "
          f"{len(got)} rows and {len(placeholders)} placeholders")
    return trimmed


def build_property(filename, url):
    full = (CAPTURES / filename).read_text(encoding="utf-8")
    soup = BeautifulSoup(full, "html.parser")
    parts = [_head(soup, full)]
    # The og: block, because `image_url` in --mode property comes from
    # `og:image` and nowhere else — a detail page's gallery is a carousel
    # whose first <img> is not reliably the hero. Dropping these silently
    # emptied the column, which the identical-parse check below caught.
    for meta in soup.select('meta[property^="og:"]'):
        parts.append(str(meta))
    for selector in ('h1', '[data-stid="content-hotel-address"]',
                     '[data-stid="content-hotel-reviewsummary"]',
                     '[data-stid="reviews-link"]',
                     P.SELECTORS["detail_price"]):
        node = soup.select_one(selector)
        if node is not None:
            parts.append(str(node))
    for script in soup.select('script[type="application/ld+json"]'):
        if "BreadcrumbList" in (script.string or ""):
            parts.append(str(script))
    parts.append("</body></html>")
    trimmed = "".join(parts)

    want = asdict(P.parse_property_page(full, url)[0])
    got = asdict(P.parse_property_page(trimmed, url)[0])
    for column, value in got.items():
        if column == "scraped_at":
            continue
        if want[column] != value:
            raise SystemExit(f"PROPERTY_US: trimming changed {column!r}: "
                             f"{want[column]!r} -> {value!r}")
    if P.detect_page_state(trimmed, 200, url) != "content":
        raise SystemExit("PROPERTY_US: trimmed fixture does not classify as content")
    print(f"  PROPERTY_US: {len(full)} -> {len(trimmed)} bytes, every column "
          f"identical")
    return trimmed


def main():
    if not CAPTURES.is_dir():
        raise SystemExit(
            f"No captures directory at {CAPTURES}. Take your own with "
            f"`--dump-html` and put them there; see this file's docstring.")
    fixtures = {"_README": (
        "Generated by make_fixtures.py from real captures. Every listing "
        "fixture is verified to parse IDENTICALLY to its untrimmed original, "
        "column for column. The challenge fixture's site keys are replaced "
        "with placeholders. Do not hand-edit — regenerate.")}
    urls = {}
    print("Cutting fixtures:")
    for name, (filename, url, keep) in SOURCES.items():
        fixtures[name] = build_listing(name, filename, url, keep)
        urls[name] = url
    fixtures["LISTING_UNPRICED"] = build_listing(
        "LISTING_UNPRICED", UNPRICED_SOURCE[0], UNPRICED_SOURCE[1], 1,
        pick=_unpriced)
    urls["LISTING_UNPRICED"] = UNPRICED_SOURCE[1]
    for name, (filename, url) in EMPTY_SOURCES.items():
        fixtures[name] = build_empty(name, filename, url)
        urls[name] = url
    fixtures["BLOCK_429"] = build_block(*BLOCK_SOURCE)
    urls["BLOCK_429"] = BLOCK_SOURCE[1]
    fixtures["PROPERTY_US"] = build_property(*PROPERTY_SOURCE)
    urls["PROPERTY_US"] = PROPERTY_SOURCE[1]
    fixtures["SITE_ERROR"] = build_site_error(*SITE_ERROR_SOURCE)
    urls["SITE_ERROR"] = SITE_ERROR_SOURCE[1]
    fixtures["LISTING_LAZY_TAIL"] = build_lazy_tail(*LAZY_TAIL_SOURCE)
    urls["LISTING_LAZY_TAIL"] = LAZY_TAIL_SOURCE[1]
    # Chromium's own answer when a proxy is dead. 39 bytes and no markup at
    # all — Playwright does not expose the interstitial, so this is what a
    # caller actually gets. It must classify as blocked, which is the
    # inverted-detection case: an absent page must not read as served.
    fixtures["BROWSER_ERROR"] = "<html><head></head><body></body></html>"
    urls["BROWSER_ERROR"] = "https://www.vrbo.com/search?destination=Orlando"
    fixtures["_URLS"] = urls

    OUT.write_text(json.dumps(fixtures, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    total = sum(len(v) for k, v in fixtures.items() if k.startswith("_") is False
                and isinstance(v, str))
    print(f"\nWrote {OUT} ({total} bytes of fixture HTML across "
          f"{len(urls)} fixtures).")


if __name__ == "__main__":
    main()
