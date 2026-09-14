#!/usr/bin/env python3
"""vrbo-scraper — Playwright edition (primary engine)

Scrapes Vrbo property listings: a search grid, or one property's own page.

    --mode listing  (default)  a /search?destination=... grid — 50 properties
                               per page, reached by scrolling an inner
                               container and paged by pressing the site's own
                               next button
    --mode property            one /pdp/lo/{id} (or /{id}, /{id}ha,
                               /{slug}/p{id}) page: the street address, the
                               location breadcrumb and the walk/drive times,
                               none of which a card states

There is deliberately no `--country` flag. The storefront IS the hostname —
vrbo.com, fewo-direkt.de, abritel.fr, bookabach.co.nz, stayz.com.au — so a
flag could only disagree with the URL it was given.

WHAT IS DIFFERENT ABOUT THIS SITE
---------------------------------
* **It reads the CLIENT before it reads the address.** Measured 2026-09-14,
  same URL, same residential exit, seconds apart: `curl` with a Chrome UA got
  HTTP 429, Playwright's BUNDLED Chromium with a real window got HTTP 429,
  and Playwright driving REAL Chrome (`channel="chrome"`) got HTTP 200 and
  the full 899 KB grid. So this engine launches real Chrome by default and
  says so loudly when it cannot — no proxy substitutes for it.

* **There is no structured data on a listing page.** 0 `application/ld+json`
  blocks, 0 `__NEXT_DATA__`, and an `__APOLLO_STATE__` holding three keys,
  because the grid arrives over client-side POSTs to `/graphql`. Rows come
  out of the DOM, anchored on the site's own `data-stid` attributes. A
  detail page does carry two JSON-LD blocks and neither is a `Product`.

* **The page body never scrolls.** `document.body.scrollHeight ===
  window.innerHeight` on every capture. The results live in an inner
  scroller; scrolling the window 12 times added nothing (18 cards before, 18
  after) while scrolling the container reached 50 of 50 in two rounds.

* **Pagination is a BUTTON, and the URL never changes.** No `link[rel=next]`,
  no `a[rel=next]`, no canonical, no hreflang anywhere. Worse, the obvious
  conventions do not fail — `&startIndex=50` and `&page=2` both answer HTTP
  200 with the counter still reading "1 - 50 of 300+" and the same first
  cards. A run built on either would report a COMPLETE result holding a
  sixth of the catalogue. So pages are turned by pressing
  `[data-stid="next-button"]`, strictly sequentially, and `--concurrency`
  above 1 is refused with that reason.

* **The listing states its own size**, `1 - 50 of 300+`, so "we merged 40
  rows off a page that said it holds 50" is arithmetic rather than a
  threshold. That gap is reported per page and recorded in the sidecar.

* **A dateless search prices every property on a DIFFERENT night.** Three
  cards in one load: Sep 28-29, Sep 16-17, Sep 24-25. Pin `startDate` and
  `endDate` in the URL for price monitoring, and read the `stay_dates`
  column before trusting any price diff.

Examples
--------
    python3 playwright_scraper.py \\
        --url "https://www.vrbo.com/search?destination=Orlando,%20Florida,%20United%20States%20of%20America" \\
        --pages 3

    python3 playwright_scraper.py \\
        --url "https://www.fewo-direkt.de/search?destination=Berlin,%20Deutschland"

    python3 playwright_scraper.py --mode property \\
        --url "https://www.vrbo.com/pdp/lo/63925107"
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS)
from product_parser import (parse_products, parse_property_page, SELECTORS,
                            NEXT_PAGE_SELECTOR, PAGE_CAP,
                            detect_bot_challenge, listing_kind, page_currency,
                            results_range, served_by_vrbo, site_host,
                            is_supported_host, source_of, unsupported_reason)
from output_writer import dedupe_by_key, finish_run, EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


# The browser channel this site actually answers. Named rather than hardcoded
# at the call site so the smoke suite can assert the three engines agree on
# it: a bundled Chromium is refused here, and an engine that quietly differed
# from its twins on this one string would be an engine that cannot fetch the
# site at all.
DEFAULT_BROWSER_CHANNEL = "chrome"

# How long to wait for a remote browser to accept the CDP connection.
#
# 150s, not the 30s this family shipped, and the difference is measured. A
# Scraping Browser endpoint provisions a browser ON DEMAND when the WebSocket
# upgrade arrives, and that upgrade was observed hanging for **121 seconds**
# before the server itself hung up. A 30s client timeout therefore abandons a
# session the server is still setting up — and the profile stays HELD by the
# half-open session: every subsequent attempt, over WebSocket and over the
# endpoint's own HTTP sibling alike, answered `500 profile_locked`, and it did
# not clear in twenty minutes.
#
# So the old default did not merely fail early, it could WEDGE THE PROFILE
# it failed on. Sitting above the server's own give-up point means the client
# is never the one that walks away first.
CDP_CONNECT_TIMEOUT_MS = 150_000


def _chrome_ua(chromium_version: str) -> str:
    """Build a desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version number: that drifts the moment a newer Chrome
    ships, and a UA claiming an older Chrome than what the JS engine, WebGL
    strings and TLS ClientHello all report is itself a mismatch a
    fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards rather than folded into shared
    state as the loop goes: dedupe that mutates a running set inside the loop
    makes the OUTPUT depend on the order pages happened to arrive in. Pages
    are strictly sequential on this site, which is exactly why keeping the
    merge order-independent costs nothing and keeps the family's contract.
    """
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    # The site's own counter for this page, verbatim and parsed: `1 - 50 of
    # 300+`. The completeness oracle (§8) — and it is per PAGE, because each
    # page states its own range.
    counter: Optional[str] = None
    first_index: Optional[int] = None
    last_index: Optional[int] = None
    total_available: Optional[int] = None
    total_is_floor: bool = False
    # How many cards the counter says this page holds and the parse did not
    # get. None when the page published no counter — an unknown gap is not a
    # gap of zero.
    gap: Optional[int] = None
    # What the scroll did: how many cards it reached and whether it SETTLED.
    # A page whose grid was still growing when the round budget ran out is a
    # floor, not a listing, and a run that reported it as complete would read
    # as a shrinking catalogue.
    scroll: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


# The lowest share of rows that must carry a price before the read is
# suspect. Measured: 50/50 on vrbo.com page 1, 40/40 on page 2, 50/50 on
# fewo-direkt.de — every card that rendered carried a price on every capture,
# so the floor sits high. A card with no price is a property with no
# availability for the dates the search implied, which does happen; 90%
# leaves room for a few of those without hiding a broken read.
PRICE_FLOOR = 90

# A page holding less than this share of the fullest page in the same run is
# reported as thin. The page size here is a steady 50 on every storefront
# measured, so the bar can sit close — but not tight, because the last page
# of a listing is legitimately short and is excluded below anyway.
THIN_PAGE_SHARE = 0.6


# ---------------------------------------------------------------------------
# page_flow, bound to Playwright
# ---------------------------------------------------------------------------
# Every decision about WHAT to do with a page — how long to wait, when to
# scroll, when a page has turned over — lives in page_flow.py so all three
# engines make it identically. What lives here is only HOW to ask this
# particular driver.
#
# The primitives are NAMED OPERATIONS rather than JavaScript (§1). Selenium's
# execute_script takes a function BODY with an explicit `return` while
# Playwright and pyppeteer take `() => expr`, so a shared module handing JS
# across this boundary would quietly acquire one driver's dialect.
def _count(page, selector: str) -> int:
    return len(page.query_selector_all(selector))


def _scroll_results(page) -> None:
    """Scroll the RESULTS container to its bottom — not the window.

    The window scroll is a no-op on this site and looks exactly like a
    working one: `document.body.scrollHeight === window.innerHeight`, so
    `window.scrollTo(0, document.body.scrollHeight)` succeeds, changes
    nothing, and a loop built on it reports a settled page holding 18 cards
    out of 50.

    Falls back to the window scroll if the container is absent, because a
    property page has no results container and a layout change should degrade
    rather than raise.
    """
    page.evaluate(
        """(selector) => {
            const el = document.querySelector(selector);
            if (el) { el.scrollTop = el.scrollHeight; }
            else { window.scrollTo(0, document.body.scrollHeight); }
        }""", SELECTORS["scroll_container"])


def _results_height(page) -> Optional[int]:
    """The results container's scroll height, or the document's if absent."""
    try:
        return page.evaluate(
            """(selector) => {
                const el = document.querySelector(selector);
                return el ? el.scrollHeight : document.body.scrollHeight;
            }""", SELECTORS["scroll_container"])
    except (PWError, PWTimeout):
        return None


# The data behind a page turn comes over POST /graphql, and that endpoint is
# rate limited SEPARATELY from the HTML: measured on this site, the search
# page kept answering HTTP 200 while every /graphql POST behind a next-press
# came back 429 with `{"error":"Too Many Requests","message":"Provisioned
# request rate has been exceeded"}`. Without counting them, a throttled page
# turn is indistinguishable from the end of the listing — which is how a
# rate-limited run comes to report "complete".
def _watch_graphql(session) -> None:
    """Start counting throttled /graphql responses on this session's page."""
    session._graphql_429 = 0

    def _on_response(response):
        try:
            if "/graphql" in response.url and response.status == 429:
                session._graphql_429 += 1
        except Exception:  # noqa: BLE001 — a listener must never break a run
            pass

    session.page.on("response", _on_response)


def _graphql_throttled(session) -> int:
    return getattr(session, "_graphql_429", 0)


def _first_card_href(page) -> Optional[str]:
    """The first result card's link, or None.

    The turnover signal for a next-press. The two more obvious signals are
    both wrong here and both were tried: the URL never changes by design, and
    the counter element is destroyed and rebuilt mid-transition, so polling
    it reads None and a wait on its text gives up on a page that was about to
    arrive (measured: a 45-second wait reported "never changed" on a press
    that had worked).
    """
    el = page.query_selector(SELECTORS["item_link"])
    return el.get_attribute("href") if el is not None else None


def _press_next(page) -> bool:
    """Press the site's own next-page button. False if it is not there.

    Scrolled into view first: the button sits below a 10,000px results
    container, and Playwright's actionability check will not click what it
    cannot bring on screen.
    """
    try:
        button = page.query_selector(NEXT_PAGE_SELECTOR)
        if button is None:
            return False
        button.scroll_into_view_if_needed(timeout=5_000)
        button.click(timeout=15_000)
        return True
    except (PWError, PWTimeout) as e:
        logger.info("Could not press the next-page button: %s", str(e)[:160])
        return False


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    """The readiness threshold, lowered to what THIS page actually holds.

    Passing the counter's own range is what keeps a short last page from
    spending the whole timeout and then reporting itself unpainted.
    """
    return page_flow.min_matches(args.mode, page_flow.expected_cards(html))


def _classify(page, html: str, status=None) -> str:
    return page_flow.classify(html, status, page.url)


def _same_url(a: str, b: str) -> bool:
    from product_parser import strip_tracking
    return strip_tracking(a or "") == strip_tracking(b or "")


# Chromium's own names for "the proxy is the problem, not the site". Matched
# on the error text because Playwright surfaces them as a generic Error.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one.

    Distinguishing this from an ordinary timeout matters because the two want
    opposite responses: a timeout deserves a retry from the same exit, while
    an unusable exit deserves a different one — retrying it unchanged just
    spends the budget on a proxy that is not going to answer.
    """
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch a browser on `pool`'s current exit; return (browser, context, page).

    Uses REAL Chrome by default, and that is the single most load-bearing
    line in this engine. Measured on this site, same address, seconds apart:
    bundled Chromium 429, real Chrome 200. If the channel is unavailable the
    launch falls back to the bundled Chromium and says clearly that the run
    is now very likely to be refused — silently falling back would turn a
    missing browser into "the site blocked us".

    Factored out so a proxy rotation can tear the whole browser down and call
    it again. Swapping the proxy under a live session would be cheaper and
    wrong: cookies a bot manager issued against one exit, replayed from
    another, are a stronger signal than either address alone.
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    channel = args.browser_channel
    if channel:
        try:
            browser = pw.chromium.launch(channel=channel, **launch_kwargs)
        except (PWError, PWTimeout) as e:
            logger.warning(
                "Could not launch the %r channel (%s) — falling back to "
                "Playwright's bundled Chromium. EXPECT HTTP 429: this site "
                "was measured refusing the bundled Chromium and serving real "
                "Chrome from the same address seconds apart. Install it with "
                "`playwright install chrome`, or point --browser-channel at "
                "one you have (msedge also works).",
                channel, str(e)[:160])
            browser = pw.chromium.launch(**launch_kwargs)
    else:
        logger.warning("--browser-channel '' launches the bundled Chromium, "
                       "which this site was measured refusing with HTTP 429.")
        browser = pw.chromium.launch(**launch_kwargs)

    ctx_kwargs = {"user_agent": _chrome_ua(browser.version),
                  "locale": args.locale,
                  "viewport": {"width": 1440, "height": 900}}
    init_script = None
    if args.fingerprint:
        # Only meaningful on this branch. Over --cdp-endpoint the Scraping
        # Browser already has its own fingerprint, and layering a second one
        # on top produces a mismatch rather than better cover.
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)",
                    fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _BrowserSession:
    """One browser + context + page, relaunchable onto a different exit.

    Exists because a rotation replaces all three handles at once, and passing
    three mutable locals through every helper is how one of them ends up
    stale.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        _watch_graphql(self)
        return self

    def relaunch(self):
        """Tear the browser down and come back on the pool's current exit.

        On a remote browser this is a no-op — its exit is not ours to change.
        """
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    try:
        browser = pw.chromium.connect_over_cdp(
            args.cdp_endpoint, timeout=args.cdp_connect_timeout * 1000)
    except (PWError, PWTimeout) as e:
        # Playwright puts the endpoint it tried into the exception text, and
        # that endpoint is a URL with a password in it — repeated five times,
        # in the message plus a four-line call log. Unmasked it lands in the
        # terminal, in CI output and in any log the run is piped to, which is
        # the one thing this project promises does not happen. The host and
        # port are KEPT: which endpoint failed is the useful half and is not
        # the secret.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"A Scraping Browser profile allows ONE live connection at a "
            f"time, so `profile_locked` here means something still holds this "
            f"`pid`. That something can be THIS tool: a connect that gives up "
            f"before the server does leaves the session half-open and the "
            f"profile wedged — measured locked for over twenty minutes "
            f"afterwards, on the endpoint's HTTP sibling as well as over "
            f"WebSocket. If that has happened, --cdp-connect-timeout is the "
            f"knob; otherwise use a different pid."
        ) from None
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    # The Scraping Browser API exposes a documented CDP domain
    # (`Captcha.setAutoSolve` / `Captcha.solve`) that clears supported
    # challenges inside the browser. Tried first when --cdp-endpoint is set;
    # this script's own detect+solve logic still runs as a fallback.
    #
    # Worth knowing what it can and cannot do here: Expedia's handler picks
    # its vendor per request, and the measured pick on this site was DataDome
    # — which neither this repo's solver nor a reCAPTCHA auto-solve covers.
    # When it picks reCAPTCHA instead, both paths apply.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve",
                         {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.waitForSolve", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA sent to 2captcha for solving."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning(
            "[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
    except Exception as e:  # noqa: BLE001
        logger.info("Captcha.setAutoSolve not available on this "
                    "--cdp-endpoint (%s) — relying on this script's own "
                    "detect+solve logic instead.", e)
    return browser, context, page


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching GLOBALLY rather than once is the point: a Playwright connection
# error repeats the endpoint five times, so a masker that handled only the
# first occurrence would print the password four times and look like it was
# working.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    """page.content() that tolerates a page mid-navigation.

    Playwright RAISES rather than returning empty while a navigation is in
    flight ("Unable to retrieve content because the page is navigating"), and
    this site's challenge handler resolves by navigating — so the one moment
    this is called is the one moment it can fail. Returns None if the page
    will not hold still, so a caller can skip a check instead of failing the
    run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — "
                               "continuing without a snapshot.", attempts)
                return None
            logger.info("Page is navigating — retrying content() in %dms "
                        "(%d/%d).", pause_ms, attempt, attempts)
            page.wait_for_timeout(pause_ms)
    return None


def handle_captcha_if_present(page, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Runs after EVERY navigation, for ANY page. The static-HTML and runtime
    reCAPTCHA detectors are run and RECONCILED against each other rather than
    short-circuited, because they can disagree about the variant and the
    parameters for one are rejected for the other.

    NOTE what this cannot help with, because on this site it is the normal
    case. Expedia's Bot-or-Not handler is a multiplexer: its own page config
    names which vendor it picked — `whichChallenge` was `datadome-challenge`
    on the measured refusal, and the same handler can serve reCAPTCHA,
    Turnstile, Arkose or a proof-of-work instead. This repo solves reCAPTCHA
    and nothing else, so `detect_page_state` reports the other picks as
    "blocked" rather than "challenge" precisely so no solve is attempted and
    nothing is charged.
    """
    html = _content_when_settled(page)
    if html is None:
        return False

    # Detected is not the same as blocking. A challenge on a page whose cards
    # are already rendered guards nothing, and counting the anchors is
    # instant — which is why this check sits here rather than after the
    # readiness wait. The other way round would cost 25 wasted seconds on a
    # page the challenge genuinely gates, where solving FIRST is what makes
    # the content appear.
    already_rendered = _count(page, _ready_selector(args))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: page.evaluate(js), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False

    if when_blocked and already_rendered > page_flow.MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d cards are already on the page "
                    "— not solving it. Pass --solve-captcha always to solve "
                    "it anyway.", challenge.kind, challenge.source,
                    already_rendered)
        return False

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting "
                   "to solve.", challenge.kind, challenge.source,
                   challenge.sitekey, challenge.action)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False

    page.evaluate(INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading page to continue.")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


def _parse_for_mode(html: str, url: str, args, page_num: int = 1) -> List:
    """Rows for this mode, always as a list even when the mode yields one.

    `page_num` is threaded through rather than defaulted, because `position`
    restarts at 1 on every page: without the page number beside it a row from
    page 2 claims the same position as one from page 1 and the two are
    indistinguishable in the output.
    """
    if args.mode == "property":
        return parse_property_page(html, url)
    return parse_products(html, url, page=page_num,
                          page_currency_code=page_currency(html))


def _scroll_the_grid(session, args, html: str, page_num: int) -> dict:
    """Scroll the results container until the grid stops growing.

    Returns the trace that goes in the sidecar. On this site this is where
    most of a page's rows come from: a first paint of 3-18 cards becomes 50
    after two rounds of scrolling the INNER container, and zero rounds of
    scrolling the window.
    """
    target = page_flow.expected_cards(html)
    before = _count(session.page, page_flow.READY_SELECTOR_LISTING)
    reached = page_flow.scroll_until_settled(
        lambda sel: _count(session.page, sel),
        lambda: _scroll_results(session.page),
        lambda: _results_height(session.page),
        session.page.wait_for_timeout,
        selector=page_flow.READY_SELECTOR_LISTING,
        target=target)
    settled = target is None or reached >= target
    logger.info("Scrolled page %d: %d card(s) at first paint, %d after "
                "scrolling%s.", page_num, before, reached,
                f" (the page says it holds {target})" if target else "")
    if not settled:
        logger.warning(
            "Page %d settled at %d card(s) but its own counter says it holds "
            "%d. Those %d are missing from this run — the grid was still "
            "filling when the scroll budget ran out, or the site served a "
            "short page.", page_num, reached, target, target - reached)
    return {"first_paint": before, "reached": reached, "target": target,
            "settled": settled}


def _fetch_one_page(session, args, pool, page_num: int, url: Optional[str]) -> PageOutcome:
    """Fetch (or turn to) one page and parse it.

    `url` is the address to navigate to for page 1, and None for every page
    after it: pages 2..N on this site are not addresses at all, they are the
    result of pressing the site's own button. Passing None is what makes that
    explicit rather than leaving a stale URL to look like it was fetched.

    Returns a PageOutcome and never raises for an EXPECTED failure — a
    timeout, a 429 refusal, a challenge page, a dead exit are all recorded on
    the outcome instead.

    Always goes through `session.page`, never a captured local: a rotation
    replaces the browser, context and page together, and a stale handle is
    exactly the bug _BrowserSession exists to prevent.
    """
    outcome = PageOutcome(page_num=page_num, url=url or session.page.url)

    has_pool = bool(pool and len(pool) > 1)
    # `RETRY_ON_BLOCKED` is CONSULTED, not merely documented — a policy
    # constant nothing reads is the same defect as dead code (§17). It is
    # True on this site, unlike a sibling repo's, because the refusal here is
    # a RATE response that clears: the same URL that answered 429 answered
    # 200 with the full grid minutes later from the same address.
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    # Counted across the whole block-retry loop, not per attempt: a page that
    # keeps coming back as a challenge would otherwise buy one solve per
    # rotation, which is how a run quietly turns into a bill.
    solves_bought = 0
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        load_failed, exit_failed = False, None

        if url is not None:
            logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
            for attempt in range(1, args.retries + 1):
                try:
                    session.page.goto(url, wait_until="domcontentloaded",
                                      timeout=60000)
                    load_failed = False
                    break
                except (PWTimeout, PWError) as e:
                    # A dead or misconfigured proxy raises PWError
                    # (net::ERR_PROXY_CONNECTION_FAILED), not PWTimeout —
                    # catching only the latter lets it escape as a traceback,
                    # which is the likeliest failure the first time anyone
                    # points --proxy-file at a real list.
                    reason = _proxy_failure(e)
                    if reason:
                        exit_failed, load_failed = reason, True
                        break  # a different exit is the only thing that helps
                    load_failed = True
                    if attempt < args.retries:
                        pause = args.retry_delay * (2 ** (attempt - 1))
                        logger.warning("Timeout loading %s (attempt %d/%d) — "
                                       "retrying in %.1fs.", url, attempt,
                                       args.retries, pause)
                        time.sleep(pause)
        else:
            logger.info("Turning to page %d/%d by pressing the site's own "
                        "next button (this listing has no page-%d address).",
                        page_num, args.pages, page_num)
            throttled_before = _graphql_throttled(session)
            turn = page_flow.advance_to_next_page(
                lambda: _press_next(session.page),
                lambda: _first_card_href(session.page),
                lambda sel: _count(session.page, sel),
                session.page.wait_for_timeout)
            if turn == page_flow.NO_BUTTON:
                # The listing genuinely ran out. A complete answer.
                outcome.state = "exhausted"
                outcome.final_url = session.page.url
                return outcome
            if turn == page_flow.NO_TURNOVER:
                # Pressed, and the grid never came back. On this site that is
                # almost always the `/graphql` POST behind the turn being
                # answered 429 — measured: every POST after a press came back
                # 429 and the site's own client gave up, leaving a collapsed
                # container and zero cards. Reporting this as the end of the
                # listing would make a throttled run say "complete" while
                # holding one page of six, so it is a FAILURE here.
                throttled = _graphql_throttled(session) - throttled_before
                outcome.state = "no_turnover"
                outcome.load_failed = True
                outcome.final_url = session.page.url
                logger.error(
                    "Pressed next for page %d and the grid never came back "
                    "within %.0fs%s. This is NOT the end of the listing — "
                    "the run is reported as PARTIAL (exit 6) rather than "
                    "complete. The data behind a page turn is fetched over "
                    "POST /graphql, which is rate limited SEPARATELY from the "
                    "HTML: the page itself still answers 200 while the turn "
                    "is refused. Raise --delay (it is %.1fs now), or spread "
                    "the load with --proxy-file.",
                    page_num, page_flow.NEXT_PAGE_TIMEOUT_MS / 1000,
                    f" — {throttled} /graphql response(s) were HTTP 429"
                    if throttled else "", args.delay)
                return outcome

        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            continue
        if load_failed:
            break

        if handle_captcha_if_present(session.page, args):
            session.page.wait_for_timeout(1000)

        html = _content_when_settled(session.page) or ""
        state = _classify(session.page, html)

        # "Not painted yet" is not a fault, and telling it apart from one is
        # the distinction §8 is about. A search page's first response is a
        # shell — the grid arrives over client-side GraphQL — so classified
        # naively it reads as something to retry, and retrying a shell buys
        # another shell. Wait for the anchor and re-classify BEFORE the retry
        # decision.
        if page_flow.is_unpainted(state, html):
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page %d is a shell the site served but has not "
                        "filled in (%d bytes, no cards) — waiting up to "
                        "%.0fs for the grid rather than spending a retry.",
                        page_num, len(html), wait_timeout / 1000)
            found = page_flow.wait_for_count(
                lambda sel: _count(session.page, sel),
                session.page.wait_for_timeout,
                _ready_selector(args), _min_matches(args, html), wait_timeout)
            if found < _min_matches(args, html):
                logger.info("The grid still had not painted after %.0fs "
                            "(%d match(es)).", wait_timeout / 1000, found)
            html = _content_when_settled(session.page) or html
            state = _classify(session.page, html)

        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session.page, args):
                session.page.wait_for_timeout(1000)
                html = _content_when_settled(session.page) or html
                state = _classify(session.page, html)
                # The VERIFIED outcome, and the only one worth reporting: a
                # "ready" task result is not evidence the token works. This
                # line is what says whether the money bought anything.
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning("The solve was NOT accepted: page %d is "
                                   "still %s. The purchase is spent.",
                                   page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty search
            # is a CORRECT one, so retrying it would spend the budget
            # re-confirming the same right answer and rotating the exit would
            # blame an address for the URL it was given.
            break

        if block_attempt < block_retries:
            pause = args.retry_delay * (block_attempt + 1)
            if has_pool:
                logger.warning("Page %d came back as %s from %s — retrying "
                               "from another exit in %.1fs (%d/%d).",
                               page_num, state, mask(pool.current), pause,
                               block_attempt + 1, block_retries)
                pool.advance(f"{state} on page {page_num}")
                session.relaunch()
                time.sleep(pause)
            else:
                # No pool, so nowhere else to go — but on this site a plain
                # wait is often what clears it, because the 429 is a rate
                # response rather than a verdict on the address. The browser
                # is NOT relaunched over --cdp-endpoint: a profile allows one
                # live connection, so reconnecting risks `profile_locked` and
                # would lose the cookies the retry is meant to build on.
                logger.warning("Page %d came back as %s — waiting %.1fs and "
                               "re-fetching through the same access path "
                               "(%d/%d). The refusal here is a RATE response "
                               "and it does clear.", page_num, state, pause,
                               block_attempt + 1, block_retries)
                time.sleep(pause)
            # A press cannot be replayed: the button is gone from a challenge
            # page. Re-navigating to page 1's URL would silently restart the
            # listing, so a blocked page-N press ends the run instead.
            if url is None:
                logger.warning("Page %d was reached by a button press, so "
                               "there is no address to re-fetch — stopping "
                               "here rather than silently restarting the "
                               "listing at page 1.", page_num)
                break

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if page_flow.counts_as_blocked(state):
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        try:
            session.page.screenshot(path=f"{args.out}_page{page_num}_debug.png")
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        served = served_by_vrbo(html or "")
        vendor = detect_bot_challenge(html or "", url=session.page.url)
        logger.error(
            "The site did not serve this request — %d bytes, %s the site's "
            "own asset hosts, saved to %s. This is exit 3, distinct from a "
            "genuinely empty result (exit 4).%s",
            len(html or ""),
            "which references" if served else "with no reference to",
            debug_html,
            (f" Tried {block_retries + 1} exit(s)." if has_pool
             else f" Re-fetched {block_retries + 1} time(s)."))
        logger.error("%s", page_flow.block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        outcome.blocked_by = vendor or ("no-response" if not html else "bot-or-not")
        outcome.final_url = session.page.url
        return outcome

    if page_flow.should_parse(state):
        selector, threshold = _ready_selector(args), _min_matches(args, html)
        content_timeout = page_flow.content_timeout_ms(args.mode)
        # A POLL, not wait_for_function. wait_for_function hands the browser a
        # STRING to evaluate, and a site whose CSP lacks `unsafe-eval`
        # refuses that outright — it took a sibling repo's run down with
        # EvalError and exit 1. A count poll is a CDP call under any CSP.
        found = page_flow.wait_for_count(
            lambda sel: _count(session.page, sel),
            session.page.wait_for_timeout, selector, threshold, content_timeout)
        session.page.wait_for_timeout(500)
        if found < threshold and args.mode == "listing":
            logger.info("No property cards appeared within %.0fs. If this "
                        "search genuinely matches nothing, that is the "
                        "expected answer and the run will report 0 rows "
                        "(exit 4).", content_timeout / 1000)

        if args.mode == "listing":
            outcome.scroll = _scroll_the_grid(session, args, html, page_num)

        html = _content_when_settled(session.page) or html

    # Dumping on success, not only on failure: a run can return the right
    # NUMBER of rows with a field silently unpopulated, and then the only way
    # to tell a parsing bug from a too-early snapshot is the exact bytes.
    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html or ""))

    products = _parse_for_mode(html or "", session.page.url, args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if args.mode == "listing":
        # The site's own counter, per page. This is the completeness oracle
        # and it is arithmetic rather than a threshold (§8): the page states
        # it holds items 1 to 50, so 40 rows is proof that ten cards never
        # loaded. One measured page-2 run had exactly that.
        rng = results_range(html or "")
        outcome.first_index, outcome.last_index = rng.first, rng.last
        outcome.total_available, outcome.total_is_floor = rng.total, rng.total_is_floor
        outcome.gap = page_flow.page_gap(html or "", len(products))
        if rng.first is not None:
            logger.info("The page's own counter says %s-%s of %s%s.",
                        rng.first, rng.last, rng.total,
                        "+ (a floor)" if rng.total_is_floor else "")
        if outcome.gap:
            logger.warning(
                "Page %d states that it holds %d propert(ies) and only %d "
                "were parsed — %d card(s) never loaded. This is not a "
                "threshold, it is the page's own arithmetic. Re-run with "
                "--dump-html to see what the browser had.",
                page_num, rng.expected_on_page, len(products), outcome.gap)

    if products and args.mode == "listing":
        priced = sum(1 for p in products if p.price is not None)
        share = 100.0 * priced / len(products)
        # Reported every time, not only when it looks wrong, so a consumer
        # gets the number rather than a threshold someone guessed.
        logger.info("Price coverage on page %d: %d/%d (%.0f%%); the measured "
                    "floor is %d%%.", page_num, priced, len(products), share,
                    PRICE_FLOOR)
        if share < PRICE_FLOOR:
            logger.warning(
                "Only %.0f%% of page %d carries a price, against a measured "
                "floor of %d%%. Every card that rendered carried one on every "
                "capture, so this is the read breaking rather than the page "
                "being unusual — re-run with --dump-html. Note this site "
                "spells the price container TWO ways (data-stid on vrbo.com, "
                "data-test-id on the local brands); a third would look "
                "exactly like this.", share, page_num, PRICE_FLOOR)

        # There is deliberately NO structured-price confirmation share here,
        # and its absence is measured rather than an omission: this site
        # publishes no structured price on any page kind, so there is no
        # second view to confirm against and a confirmation threshold would
        # describe nothing. What IS worth reporting is the image share, the
        # one column this site makes sparse on purpose.
        with_image = sum(1 for p in products if p.image_url)
        logger.info("Card images on page %d: %d/%d (%.0f%%). Low is NORMAL: "
                    "the gallery is not mounted below the fold, so 41 of 50 "
                    "cards carried no <img> element at all on the measured "
                    "page. A null here is an unmounted gallery, not a "
                    "property without photos.",
                    page_num, with_image, len(products),
                    100.0 * with_image / len(products))

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        debug_png = f"{args.out}_page{page_num}_debug.png"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        try:
            session.page.screenshot(path=debug_png)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw "
                       "to %s and %s. Open the .png to see it.",
                       debug_html, debug_png)

    outcome.products = products
    outcome.final_url = session.page.url
    return outcome


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    dedupe_key = "sku"
    stop_reason = "single_page_mode" if args.mode == "property" else "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None

    # Refused, not silently honoured. A listing here has no per-page address,
    # so there is nothing to hand a second worker — and saying so is the
    # point: running one worker quietly would look like the flag did
    # something (§18).
    if args.concurrency > 1:
        refusal = page_flow.concurrency_refusal(args.url)
        logger.warning("--concurrency %d is refused: %s.",
                       args.concurrency, refusal)

    session = None
    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
        try:
            first = _fetch_one_page(session, args, pool, 1, args.url)
            outcomes.append(first)

            if not first.ok:
                stop_reason = ("page_load_timeout" if first.load_failed
                               else f"blocked_{first.blocked_by}")
                blocked = first.blocked_by is not None
            elif args.mode == "property":
                pass  # one page is the whole run
            else:
                seen_keys.update(p.sku for p in first.products if p.sku is not None)
                for page_num in range(2, args.pages + 1):
                    if page_flow.page_cap_reached(page_num):
                        logger.warning(
                            "Stopping at the %d-page cap. Every page past the "
                            "first costs a sequential button press and a "
                            "fresh GraphQL round trip on this site, so a "
                            "deeper walk is a bill rather than a dataset — "
                            "narrow the search with the site's own filters "
                            "instead.", PAGE_CAP)
                        stop_reason = "page_cap"
                        break
                    # A new exit per page is what actually spreads a run's
                    # volume — but it cannot be done here, and saying so is
                    # better than doing it wrong: the next page exists only
                    # inside THIS browser's session, so relaunching on
                    # another exit would lose the listing and silently
                    # restart it at page 1.
                    if pool and pool.rotates_per_page() and page_num == 2:
                        logger.warning(
                            "--proxy-rotate per-page cannot be honoured on a "
                            "Vrbo listing: page %d exists only inside this "
                            "browser's session (it is reached by pressing the "
                            "site's own button, not by an address), so "
                            "relaunching on another exit would restart the "
                            "listing at page 1. Holding the current exit for "
                            "the whole run.", page_num)

                    time.sleep(args.delay)
                    outcome = _fetch_one_page(session, args, pool, page_num, None)
                    outcomes.append(outcome)

                    if outcome.state == "exhausted":
                        logger.info("The site offered no further page after "
                                    "page %d — treating that as the end of "
                                    "the listing.", page_num - 1)
                        stop_reason = "pagination_exhausted"
                        outcomes.pop()  # nothing was fetched; do not count it
                        break
                    if outcome.state == "no_turnover":
                        # Deliberately NOT "pagination_exhausted", which is a
                        # COMPLETE stop reason: a throttled turn is a partial
                        # run, not a finished one.
                        stop_reason = "next_page_throttled"
                        outcomes.pop()
                        break
                    if not outcome.ok:
                        stop_reason = ("page_load_timeout" if outcome.load_failed
                                       else f"blocked_{outcome.blocked_by}")
                        blocked = outcome.blocked_by is not None
                        break

                    # Whether this page contributed anything not already
                    # seen. The authoritative dedupe happens once, after the
                    # loop, in page order; this running check exists because
                    # the termination condition is inherently sequential.
                    fresh_count = sum(1 for p in outcome.products
                                      if p.sku is None or p.sku not in seen_keys)
                    seen_keys.update(p.sku for p in outcome.products
                                     if p.sku is not None)
                    if not fresh_count:
                        # A property of the DATA, not of a CSS selector that
                        # may have been renamed (§7). On this site it is also
                        # the signal that a next-press did not actually take.
                        logger.info("Page %d added no rows not already seen — "
                                    "treating that as the end of the "
                                    "listing.", page_num)
                        stop_reason = "no_new_products"
                        break
        finally:
            if session is not None:
                session.close()

    # Merge once, in PAGE order — not in the order pages happened to finish.
    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            # Measured NOT to happen on a healthy run: page 1 and page 2 of
            # one Orlando search shared 0 of 50 titles. So any non-zero count
            # here is worth reading — the likeliest cause is a next-press
            # that did not take and re-parsed the page it was already on.
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    total_available = next((o.total_available for o in outcomes
                            if o.total_available is not None), None)
    total_is_floor = next((o.total_is_floor for o in outcomes
                           if o.total_available is not None), False)

    if args.mode == "listing" and all_rows:
        # Completeness, checked over the MERGED result rather than per page —
        # a per-page check cannot see a gap BETWEEN two pages, which is
        # exactly where a short page hides.
        counts = [(o.page_num, len(o.products)) for o in outcomes if o.ok]
        fullest = max((n for _, n in counts), default=0)
        last_page = max((p for p, _ in counts), default=0)
        thin = [(p, n) for p, n in counts
                if fullest and n < THIN_PAGE_SHARE * fullest and p != last_page]
        if thin:
            logger.warning(
                "Page(s) %s came back much thinner than the fullest page "
                "(%d rows): %s. A half-scrolled grid looks like this — "
                "re-run with --dump-html to check those pages.",
                ", ".join(str(p) for p, _ in thin), fullest,
                ", ".join("page %d: %d" % (p, n) for p, n in thin))

        gaps = {o.page_num: o.gap for o in outcomes if o.gap}
        if gaps:
            logger.warning(
                "The site's own counter says %d card(s) never loaded across "
                "page(s) %s. That is arithmetic, not an estimate — see "
                "cards_missing in the sidecar.",
                sum(gaps.values()), ", ".join(str(p) for p in sorted(gaps)))
            # And it makes the run PARTIAL, not complete. Every page fetched
            # was fetched, so the naive reading is "complete" — but a
            # consumer reading status=complete beside 31 missing cards would
            # take a hole in the catalogue for a delisting. Measured: a
            # healthy page reaches its stated 50 of 50 every time, so a gap
            # is abnormal rather than routine (§8: blocked != empty !=
            # partial).
            if stop_reason in ("completed", "pagination_exhausted",
                               "no_new_products"):
                stop_reason = "cards_missing"
        if total_available:
            logger.info("This search holds %s%d propert(ies); this run took "
                        "%d (%.1f%%).", "at least " if total_is_floor else "",
                        total_available, len(all_rows),
                        100.0 * len(all_rows) / total_available)

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    # One-per-run context, in the sidecar rather than repeated down a column.
    extra = None
    if args.mode == "listing":
        scrolls = {o.page_num: o.scroll for o in outcomes if o.scroll}
        counters = {o.page_num: f"{o.first_index}-{o.last_index}"
                    for o in outcomes if o.first_index is not None}
        cards_missing = {o.page_num: o.gap for o in outcomes if o.gap}
        unsettled = sorted(n for n, s in scrolls.items()
                           if s and not s.get("settled"))
        extra = {"scroll": scrolls, "page_counters": counters,
                 "cards_missing": cards_missing,
                 "results_total": total_available,
                 "results_total_is_floor": total_is_floor,
                 "pages_still_growing": unsettled}
        if unsettled:
            logger.warning(
                "Page(s) %s were still filling when the scroll budget ran "
                "out, so their row counts are floors rather than the "
                "listing.", ", ".join(str(n) for n in unsettled))

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=source_of(final_url),
                      start_url=args.url, final_url=final_url,
                      extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="Vrbo property scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="A Vrbo URL: a search grid "
                        "(/search?destination=...) or one property "
                        "(/pdp/lo/{id}, /{id}, /{id}ha or /{slug}/p{id}) "
                        "with --mode property. The storefront IS the "
                        "hostname — vrbo.com, fewo-direkt.de, abritel.fr, "
                        "bookabach.co.nz and stayz.com.au are all supported "
                        "and all serve identical markup. Required, unless "
                        "VRBO_URL is set in the environment or in .env.")
    p.add_argument("--mode", choices=["listing", "property"], default="listing",
                   help="listing (default): a /search grid — 50 properties "
                        "per page, reached by scrolling an inner container. "
                        "property: one property page, which adds the street "
                        "address and the location breadcrumb that a card does "
                        "not publish. --pages applies to listing only.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. In --mode property "
                        "the column is filled from the page's own "
                        "BreadcrumbList JSON-LD, so it is rarely empty there; "
                        "a search card publishes no breadcrumb, so pass one "
                        "if you want the column filled on a listing run.")
    p.add_argument("--pages", type=int, default=1,
                   help=f"Number of listing pages to walk (default 1, cap "
                        f"{PAGE_CAP}). Each page past the first is reached by "
                        f"PRESSING the site's own next button — this listing "
                        f"has no per-page address, and the obvious "
                        f"conventions (&page=2, &startIndex=50) silently "
                        f"return page 1 rather than failing. So pages are "
                        f"strictly sequential and --concurrency cannot help.")
    p.add_argument("--delay", type=float, default=5.0,
                   help="Delay between pages, seconds (default 5.0 — higher "
                        "than the family default, on purpose). The data "
                        "behind a page turn comes over POST /graphql, which "
                        "is rate limited separately from the HTML: measured, "
                        "the search page kept answering 200 while every "
                        "/graphql POST behind a next-press came back 429. "
                        "Slowing down is the cheapest thing that works, and "
                        "this is the lever.")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for family compatibility and REFUSED above "
                        "1, with the reason: a Vrbo listing has no per-page "
                        "address, so page 5 exists only behind four "
                        "sequential button presses and there is nothing to "
                        "hand a second worker. Run several searches in "
                        "parallel instead, one process each.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "The pause between attempts doubles each time. A page "
                        "that comes back EMPTY is not retried — see "
                        "page_flow.STATE_POLICY — because an empty search is "
                        "a correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="vrbo_products", help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US). It does NOT decide "
                        "the language or the currency: the STOREFRONT does, "
                        "and the storefront is the hostname. This only "
                        "affects what the browser claims about itself.")
    p.add_argument("--browser-channel", default=DEFAULT_BROWSER_CHANNEL,
                   metavar="CHANNEL",
                   help=f"Which installed browser to drive (default "
                        f"{DEFAULT_BROWSER_CHANNEL!r}). THE flag that decides "
                        f"whether this works: measured on the same address "
                        f"seconds apart, Playwright's bundled Chromium was "
                        f"answered HTTP 429 and real Chrome HTTP 200 with the "
                        f"full grid. Install it with `playwright install "
                        f"chrome`. Pass an empty string to use the bundled "
                        f"Chromium anyway.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line (# comments and "
                        "blank lines skipped) to rotate across. Wins over "
                        "--proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES),
                   default="per-run",
                   help="per-run (default): one exit for the whole run. "
                        "per-page is accepted but cannot be honoured mid-"
                        "listing here — the next page lives inside the "
                        "current browser session, so rotating would restart "
                        "the listing at page 1. The run says so when it "
                        "happens.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup, so concurrent runs do "
                        "not all begin on the first exit in the file.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back refused (HTTP 429), retry it "
                        "from this many OTHER exits before giving up (default "
                        "2). Needs a pool of more than one; ignored "
                        "otherwise. Worth knowing on this site: the refusal "
                        "is a RATE response that clears on its own, so "
                        "--delay is usually the better lever.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found. Off "
                        "by default so a failed run can't overwrite a good "
                        "result with an empty one; exit code is 4 either way.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's "
                        "Fingerprint API and apply it to the launched "
                        "browser. Needs --twocaptcha-key. Ignored with "
                        "--cdp-endpoint, where the Scraping Browser supplies "
                        "its own.")
    # ONE OS-family tag, not a list — and the default is what makes
    # --fingerprint work at all. It shipped as "Windows,Chrome,Desktop" across
    # this family, which the API rejects with HTTP 400, so --fingerprint
    # failed on every invocation in four repos at once (§17).
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400. Use --fp-country to narrow further. "
                        "(default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country — a US fingerprint on a "
                        "German IP is a contradiction.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current "
                        "JSON API (api.2captcha.com/createTask); v1 is the "
                        "legacy in.php/res.php pair.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. Note what "
                        "neither setting reaches: Expedia's challenge handler "
                        "picks its vendor per request, and the measured pick "
                        "on this site was DataDome, which this repo has no "
                        "solver for. Those are reported as blocked and "
                        "nothing is charged.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or "
                        "0.9 — the API only accepts these three). Ignored for "
                        "v2 widgets.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP "
                        "instead of launching one locally, e.g. "
                        "ws://user:pass@host:port — the Scraping Browser API "
                        "endpoint, or any browser that exposes a CDP URL. "
                        "--proxy, --browser-channel and --headless/--headful "
                        "are ignored when this is set.")
    p.add_argument("--cdp-connect-timeout", type=float,
                   default=CDP_CONNECT_TIMEOUT_MS / 1000, metavar="SECONDS",
                   help=f"How long to wait for --cdp-endpoint to accept the "
                        f"connection (default {CDP_CONNECT_TIMEOUT_MS // 1000}). "
                        f"Deliberately high: a Scraping Browser provisions a "
                        f"browser when the WebSocket upgrade arrives, and one "
                        f"was measured taking 121s before the SERVER gave up. "
                        f"Giving up earlier than the server does leaves the "
                        f"profile held by a half-open session — measured "
                        f"`profile_locked` on every later attempt, for over "
                        f"twenty minutes.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure. Useful when the row count is "
                        "right but a column comes back empty — see "
                        "TROUBLESHOOTING.md.")
    # HEADFUL by default. The measured discriminator on this site is the
    # browser BUILD rather than the window (bundled Chromium was refused
    # headful), but a real window costs nothing next to real Chrome and
    # removes one variable from a refusal.
    p.add_argument("--headful", dest="headless", action="store_false",
                   default=False,
                   help="Run with a real browser window. THE DEFAULT here.")
    p.add_argument("--headless", dest="headless", action="store_true",
                   help="Run headless. Not measured to work on this site; "
                        "the measured discriminator is --browser-channel.")
    args = p.parse_args()
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url from the
    # environment or .env when the flag was not given. An explicit flag wins.
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and VRBO_URL is not set in the environment "
                "or in .env.")
    why = unsupported_reason(args.url)
    if why:
        # Refused rather than attempted. The parser's card selectors, its
        # property-path patterns and its pagination are all this site's, so
        # pointing it at another travel site would not fail loudly — it would
        # return zero rows and look like an empty search.
        p.error(why)
    kind = listing_kind(args.url)
    if args.mode == "property" and kind != "property":
        p.error(f"--mode property expects a property URL (/pdp/lo/{{id}}, "
                f"/{{id}}, /{{id}}ha or /{{slug}}/p{{id}}); {args.url!r} is a "
                f"{kind} page.")
    if args.mode == "listing" and kind == "property":
        p.error(f"{args.url!r} is a single property page. Use --mode property "
                f"for it, or pass a /search?destination=... URL.")
    if args.mode == "property" and args.pages != 1:
        logger.warning("--pages %d is ignored in --mode property: there is "
                       "one page to read. The run status will say "
                       "single_page_mode.", args.pages)
        args.pages = 1
    if args.mode == "listing" and args.pages > PAGE_CAP:
        logger.warning("--pages %d is above this scraper's %d-page cap; it "
                       "will stop there.", args.pages, PAGE_CAP)
    if args.mode == "listing" and "startDate=" not in (args.url or ""):
        # Said out loud because it silently changes what the numbers MEAN. A
        # dateless search prices every property on its own cheapest night, so
        # the prices in one run are not comparable to each other and a row
        # will "change price" between runs when only the date moved.
        logger.warning(
            "This search carries no dates, so the site will quote each "
            "property its OWN cheapest one-night stay — measured: three "
            "cards in one load priced for Sep 28-29, Sep 16-17 and Sep 24-25. "
            "Those prices are NOT comparable to each other. Add startDate and "
            "endDate to the URL for price monitoring, and read the stay_dates "
            "column either way.")
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint "
                     "API uses the same key, though it's a separate "
                     "subscription from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second one on top creates a mismatch "
                       "rather than better cover.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). `profile_locked` means another run still holds this
        # `pid`, and a harness that sees exit 1 goes looking for a bug in the
        # scraper instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
