#!/usr/bin/env python3
"""vrbo-scraper — pyppeteer edition

A parity engine. pyppeteer is effectively unmaintained and its own README
points at Playwright; this exists so the family has three drivers behind one
row schema, not because it is the better choice.

It must behave identically to `playwright_scraper.py`: the same flags, the
same rows in the same column order, the same exit codes and the same run
status.

Read playwright_scraper.py's docstring for what is different about this site.
One thing is different about this ENGINE, and it decides whether it works at
all:

**pyppeteer downloads its own Chromium, and this site refuses that build.**
Measured on the same residential address seconds apart: a bundled Chromium
was answered HTTP 429 and a real Chrome HTTP 200 with the full grid. So
`--chromium-path` is not a convenience here, it is the flag that makes this
engine usable — point it at an installed Chrome:

    --chromium-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    --chromium-path /usr/bin/google-chrome

Without it the run will almost certainly report exit 3, and it says so at
startup rather than leaving the reader to discover it from a blocked page.

`--fingerprint` and `--locale` are absent from this engine and present in the
others; that difference is documented in the README and asserted in the
suite, so closing it needs a README edit rather than a quiet patch.

    python3 puppeteer_scraper.py \\
        --url "https://www.vrbo.com/search?destination=Orlando,%20Florida,%20United%20States%20of%20America" \\
        --pages 2 \\
        --chromium-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"""

import argparse
import asyncio
import concurrent.futures
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

# At module level, deliberately, and not inside the launch path. The offline
# suite guards `import puppeteer_scraper` behind try/except ImportError and
# REPORTS the skip, and CI's engine-smoke job fails on any reported skip —
# that whole mechanism only works if importing this module actually requires
# the driver. With the import hidden inside _Session.open(), the module
# imports cleanly with no pyppeteer installed at all, the group never skips,
# and CI cannot notice a broken import (§10).
from pyppeteer import launch, connect

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS)
from product_parser import (parse_products, parse_property_page, SELECTORS,
                            NEXT_PAGE_SELECTOR, PAGE_CAP,
                            detect_bot_challenge, listing_kind, page_currency,
                            results_range, served_by_vrbo, source_of,
                            unsupported_reason)
from output_writer import dedupe_by_key, finish_run
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, split_credentials,
                        mask, ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

PRICE_FLOOR = 90
THIN_PAGE_SHARE = 0.6

# Every await in this file goes through the bridge below with a timeout, so a
# hung remote call ends the operation instead of the run. pyppeteer provides
# no connect timeout of its own and its page methods' `timeout` option does
# not cover a browser that has stopped answering at all.
DEFAULT_OP_TIMEOUT = 120
CONNECT_TIMEOUT = 30


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Exists so this engine can reuse page_flow.py unchanged. That module holds
    the policy all three engines must share, and it is written against plain
    synchronous callables — the right shape for two of the three drivers.
    Bridging here keeps the policy in one place rather than growing an async
    copy of it that would drift.

    The second benefit is what the family's rules actually require: every
    call gets an explicit, enforced timeout. `.result(timeout)` returns
    control even when the browser never answers, which pyppeteer's own API
    does not offer.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each one at ERROR level, AFTER a successful run has
        # printed its results. Five of those under a "Saved 95 products" line
        # read as a failed run. Only that shape is swallowed; anything else
        # still gets the default handler, because silencing the loop
        # wholesale would hide real faults.
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        # BOTH, not one or the other. asyncio puts its own words in
        # `message` ("Future exception was never retrieved") and the
        # library's in `exception`, and an `or` between them looks at the
        # exception and never sees the message — which is why these kept
        # printing after they were "handled".
        message = " | ".join(
            str(context.get(k)) for k in ("exception", "message")
            if context.get(k))
        if any(m in message for m in (
                "Target closed", "Connection closed",
                "Task was destroyed but it is pending",
                "Future exception was never retrieved",
                "No session with given id",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING whatever it still has in flight.

        Stopping the loop outright leaves pyppeteer's background tasks
        pending — its websocket reader and keepalive — and asyncio then
        prints "Task was destroyed but it is pending!" plus a traceback for
        each. That happens AFTER the output is written, so the run is fine
        and the log looks like a crash.

        Cancelling first is the fix, and it has to happen ON the loop thread —
        `call_soon_threadsafe` is what gets it there.
        """
        def _cancel_and_stop():
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task(self.loop)]
            for task in pending:
                task.cancel()
            if pending:
                logger.debug("Cancelled %d pending pyppeteer task(s) on "
                             "teardown.", len(pending))
            self.loop.stop()

        self.loop.call_soon_threadsafe(_cancel_and_stop)
        self._thread.join(timeout=5)


@dataclass
class PageOutcome:
    """What one page produced. Mirrors playwright_scraper.PageOutcome."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    counter: Optional[str] = None
    first_index: Optional[int] = None
    last_index: Optional[int] = None
    total_available: Optional[int] = None
    total_is_floor: bool = False
    gap: Optional[int] = None
    scroll: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA from the browser's own reported version.

    `browser.version()` returns "HeadlessChrome/115.0.0.0"; the marketing
    part is what a real Chrome would send.
    """
    number = version.split("/")[-1] if "/" in version else version
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{number} Safari/537.36")


class _Session:
    """One pyppeteer browser + page, relaunchable onto a different exit."""

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None

    def open(self):
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            # pyppeteer's browserWSEndpoint takes the full ws://user:pass@host
            # form and authenticates on the WebSocket upgrade, so an
            # authenticated Scraping Browser endpoint works here — unlike
            # Selenium's debuggerAddress, which has nowhere to put a password.
            self.browser = self.bridge.run(
                connect(browserWSEndpoint=self.args.cdp_endpoint,
                        ignoreHTTPSErrors=True), timeout=CONNECT_TIMEOUT)
            self.page = self.bridge.run(self.browser.newPage())
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage",
                       "--disable-blink-features=AutomationControlled"]
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
            logger.info("Using the browser at %s instead of pyppeteer's own.",
                        self.args.chromium_path)
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials go through page.authenticate(), never onto the
            # command line: --proxy-server= becomes part of the browser's
            # argv, readable by anything that can run `ps`.
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))

        # handleSIGINT/TERM/HUP off, and not for tidiness: pyppeteer installs
        # signal handlers inside launch(), and `signal.signal` raises "signal
        # only works in main thread of the main interpreter" because the
        # event loop here lives on a worker thread. Teardown is handled by
        # _Session.close() in scrape()'s finally block instead.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        version = self.bridge.run(self.browser.version())
        self.bridge.run(self.page.setUserAgent(_chrome_ua(version)))
        # 1440x900, matching the captures. The split-view layout on this site
        # changes how many cards paint first, so keeping the window the
        # measured size keeps the README's numbers meaningful.
        self.bridge.run(self.page.setViewport({"width": 1440, "height": 900}))
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        return self

    def relaunch(self):
        if self.remote:
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to pyppeteer
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. pyppeteer takes `() => expr` like
# Playwright and unlike Selenium, which is exactly why page_flow names
# operations rather than passing JavaScript across the boundary (§1).
def _count(session, selector: str) -> int:
    try:
        return len(session.bridge.run(session.page.querySelectorAll(selector)))
    except Exception as e:  # noqa: BLE001
        logger.debug("count(%s) failed: %s", selector, e)
        return 0


def _sleep(ms: int) -> None:
    time.sleep(ms / 1000.0)


def _content(session) -> Optional[str]:
    try:
        return session.bridge.run(session.page.content())
    except Exception as e:  # noqa: BLE001
        logger.debug("content() unavailable (page navigating?): %s", e)
        return None


def _current_url(session) -> str:
    try:
        return session.bridge.run(session.page.evaluate("() => location.href"))
    except Exception:  # noqa: BLE001
        return ""


def _scroll_results(session) -> None:
    """Scroll the RESULTS container to its bottom — not the window.

    The window scroll is a no-op on this site and looks exactly like a
    working one: `document.body.scrollHeight === window.innerHeight`.
    """
    try:
        session.bridge.run(session.page.evaluate(
            """(selector) => {
                const el = document.querySelector(selector);
                if (el) { el.scrollTop = el.scrollHeight; }
                else { window.scrollTo(0, document.body.scrollHeight); }
            }""", SELECTORS["scroll_container"]))
    except Exception as e:  # noqa: BLE001
        logger.debug("scroll failed: %s", e)


def _results_height(session) -> Optional[int]:
    try:
        return session.bridge.run(session.page.evaluate(
            """(selector) => {
                const el = document.querySelector(selector);
                return el ? el.scrollHeight : document.body.scrollHeight;
            }""", SELECTORS["scroll_container"]))
    except Exception:  # noqa: BLE001
        return None


def _first_card_href(session) -> Optional[str]:
    """The first result card's link, or None. The page-turn signal."""
    try:
        return session.bridge.run(session.page.evaluate(
            """(selector) => {
                const el = document.querySelector(selector);
                return el ? el.getAttribute('href') : null;
            }""", SELECTORS["item_link"]))
    except Exception:  # noqa: BLE001
        return None


def _press_next(session) -> bool:
    """Press the site's own next-page button. False if it is not there."""
    try:
        pressed = session.bridge.run(session.page.evaluate(
            """(selector) => {
                const el = document.querySelector(selector);
                if (!el) { return false; }
                el.scrollIntoView({block: 'center'});
                el.click();
                return true;
            }""", NEXT_PAGE_SELECTOR))
        return bool(pressed)
    except Exception as e:  # noqa: BLE001
        logger.info("Could not press the next-page button: %s", str(e)[:160])
        return False


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    return page_flow.min_matches(args.mode, page_flow.expected_cards(html))


def _classify(session, html: str, status=None) -> str:
    # `status` POSITIONAL and second, matching the other two engines and the
    # callee's real signature (§17).
    return page_flow.classify(html, status, _current_url(session))


def _parse_for_mode(html: str, url: str, args, page_num: int = 1) -> List:
    if args.mode == "property":
        return parse_property_page(html, url)
    return parse_products(html, url, page=page_num,
                          page_currency_code=page_currency(html))


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved."""
    html = _content(session)
    if html is None:
        return False

    already_rendered = _count(session, _ready_selector(args))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    url = _current_url(session)
    html_challenge = detect_recaptcha_v3(html, url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: session.bridge.run(session.page.evaluate(js)), page_url=url)
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

    session.bridge.run(session.page.evaluate(INJECT_TOKEN_JS, token))
    logger.info("Token injected. Reloading page to continue.")
    _sleep(1500)
    try:
        session.bridge.run(session.page.reload(
            {"waitUntil": "domcontentloaded", "timeout": 60000}))
    except Exception as e:  # noqa: BLE001
        logger.warning("Reload after the solve failed: %s", e)
    return True


def _scroll_the_grid(session, args, html: str, page_num: int) -> dict:
    target = page_flow.expected_cards(html)
    before = _count(session, page_flow.READY_SELECTOR_LISTING)
    reached = page_flow.scroll_until_settled(
        lambda sel: _count(session, sel),
        lambda: _scroll_results(session),
        lambda: _results_height(session),
        _sleep,
        selector=page_flow.READY_SELECTOR_LISTING,
        target=target)
    settled = target is None or reached >= target
    logger.info("Scrolled page %d: %d card(s) at first paint, %d after "
                "scrolling%s.", page_num, before, reached,
                f" (the page says it holds {target})" if target else "")
    if not settled:
        logger.warning(
            "Page %d settled at %d card(s) but its own counter says it holds "
            "%d. Those %d are missing from this run.",
            page_num, reached, target, target - reached)
    return {"first_paint": before, "reached": reached, "target": target,
            "settled": settled}


def _fetch_one_page(session, args, pool, page_num: int,
                    url: Optional[str]) -> PageOutcome:
    outcome = PageOutcome(page_num=page_num, url=url or _current_url(session))

    has_pool = bool(pool and len(pool) > 1)
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    solves_bought = 0
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        load_failed = False

        if url is not None:
            logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
            for attempt in range(1, args.retries + 1):
                try:
                    session.bridge.run(session.page.goto(
                        url, {"waitUntil": "domcontentloaded",
                              "timeout": 60000}))
                    load_failed = False
                    break
                except Exception as e:  # noqa: BLE001
                    load_failed = True
                    if attempt < args.retries:
                        pause = args.retry_delay * (2 ** (attempt - 1))
                        logger.warning("Timeout loading %s (attempt %d/%d: "
                                       "%s) — retrying in %.1fs.", url,
                                       attempt, args.retries, str(e)[:120],
                                       pause)
                        time.sleep(pause)
        else:
            logger.info("Turning to page %d/%d by pressing the site's own "
                        "next button (this listing has no page-%d address).",
                        page_num, args.pages, page_num)
            turn = page_flow.advance_to_next_page(
                lambda: _press_next(session),
                lambda: _first_card_href(session),
                lambda sel: _count(session, sel),
                _sleep)
            if turn == page_flow.NO_BUTTON:
                outcome.state = "exhausted"
                outcome.final_url = _current_url(session)
                return outcome
            if turn == page_flow.NO_TURNOVER:
                # NOT the end of the listing — see the Playwright engine for
                # the measurement. Reporting a throttle as "exhausted" would
                # make a rate-limited run say "complete" while holding one
                # page of six.
                outcome.state = "no_turnover"
                outcome.load_failed = True
                outcome.final_url = _current_url(session)
                logger.error(
                    "Pressed next for page %d and the grid never came back "
                    "within %.0fs. This is NOT the end of the listing — the "
                    "run is reported as PARTIAL (exit 6). The data behind a "
                    "page turn is fetched over POST /graphql, which is rate "
                    "limited SEPARATELY from the HTML. Raise --delay (it is "
                    "%.1fs now), or spread the load with --proxy-file.",
                    page_num, page_flow.NEXT_PAGE_TIMEOUT_MS / 1000, args.delay)
                return outcome

        if load_failed:
            break

        if handle_captcha_if_present(session, args):
            _sleep(1000)

        html = _content(session) or ""
        state = _classify(session, html)

        if page_flow.is_unpainted(state, html):
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page %d is a shell the site served but has not "
                        "filled in (%d bytes, no cards) — waiting up to "
                        "%.0fs for the grid rather than spending a retry.",
                        page_num, len(html), wait_timeout / 1000)
            page_flow.wait_for_count(
                lambda sel: _count(session, sel), _sleep,
                _ready_selector(args), _min_matches(args, html), wait_timeout)
            html = _content(session) or html
            state = _classify(session, html)

        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session, args):
                _sleep(1000)
                html = _content(session) or html
                state = _classify(session, html)
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning("The solve was NOT accepted: page %d is "
                                   "still %s. The purchase is spent.",
                                   page_num, state)

        if not page_flow.should_retry(state):
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
                logger.warning("Page %d came back as %s — waiting %.1fs and "
                               "re-fetching (%d/%d). The refusal here is a "
                               "RATE response and it does clear.",
                               page_num, state, pause, block_attempt + 1,
                               block_retries)
                time.sleep(pause)
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
            session.bridge.run(session.page.screenshot(
                {"path": f"{args.out}_page{page_num}_debug.png"}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        served = served_by_vrbo(html or "")
        vendor = detect_bot_challenge(html or "", url=_current_url(session))
        logger.error(
            "The site did not serve this request — %d bytes, %s the site's "
            "own asset hosts, saved to %s. This is exit 3, distinct from a "
            "genuinely empty result (exit 4).", len(html or ""),
            "which references" if served else "with no reference to",
            debug_html)
        if not args.chromium_path and not args.cdp_endpoint:
            logger.error(
                "This engine launched pyppeteer's OWN Chromium, which is the "
                "build this site was measured refusing. Pass --chromium-path "
                "pointing at an installed Chrome before concluding anything "
                "about the address.")
        logger.error("%s", page_flow.block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        outcome.blocked_by = vendor or ("no-response" if not html else "bot-or-not")
        outcome.final_url = _current_url(session)
        return outcome

    if page_flow.should_parse(state):
        selector, threshold = _ready_selector(args), _min_matches(args, html)
        content_timeout = page_flow.content_timeout_ms(args.mode)
        found = page_flow.wait_for_count(
            lambda sel: _count(session, sel), _sleep, selector, threshold,
            content_timeout)
        _sleep(500)
        if found < threshold and args.mode == "listing":
            logger.info("No property cards appeared within %.0fs. If this "
                        "search genuinely matches nothing, that is the "
                        "expected answer and the run will report 0 rows "
                        "(exit 4).", content_timeout / 1000)
        if args.mode == "listing":
            outcome.scroll = _scroll_the_grid(session, args, html, page_num)
        html = _content(session) or html

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html or ""))

    products = _parse_for_mode(html or "", _current_url(session), args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if args.mode == "listing":
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
                "threshold, it is the page's own arithmetic.",
                page_num, rng.expected_on_page, len(products), outcome.gap)

    if products and args.mode == "listing":
        priced = sum(1 for p in products if p.price is not None)
        share = 100.0 * priced / len(products)
        logger.info("Price coverage on page %d: %d/%d (%.0f%%); the measured "
                    "floor is %d%%.", page_num, priced, len(products), share,
                    PRICE_FLOOR)
        if share < PRICE_FLOOR:
            logger.warning(
                "Only %.0f%% of page %d carries a price, against a measured "
                "floor of %d%%. Note this site spells the price container TWO "
                "ways (data-stid on vrbo.com, data-test-id on the local "
                "brands); a third would look exactly like this.",
                share, page_num, PRICE_FLOOR)
        with_image = sum(1 for p in products if p.image_url)
        logger.info("Card images on page %d: %d/%d (%.0f%%). Low is NORMAL: "
                    "the gallery is not mounted below the fold.",
                    page_num, with_image, len(products),
                    100.0 * with_image / len(products))

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.warning("0 rows parsed — saved what the browser actually saw "
                       "to %s.", debug_html)

    outcome.products = products
    outcome.final_url = _current_url(session)
    return outcome


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    stop_reason = "single_page_mode" if args.mode == "property" else "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint "
                       "the remote browser has its own exit.")
        pool = None

    if args.concurrency > 1:
        logger.warning("--concurrency %d is refused: %s.", args.concurrency,
                       page_flow.concurrency_refusal(args.url))

    bridge = _AsyncBridge()
    session = _Session(bridge, args, pool).open()
    try:
        first = _fetch_one_page(session, args, pool, 1, args.url)
        outcomes.append(first)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None
        elif args.mode == "property":
            pass
        else:
            seen_keys.update(p.sku for p in first.products if p.sku is not None)
            for page_num in range(2, args.pages + 1):
                if page_flow.page_cap_reached(page_num):
                    logger.warning("Stopping at the %d-page cap.", PAGE_CAP)
                    stop_reason = "page_cap"
                    break
                if pool and pool.rotates_per_page() and page_num == 2:
                    logger.warning(
                        "--proxy-rotate per-page cannot be honoured on a Vrbo "
                        "listing: page %d exists only inside this browser's "
                        "session.", page_num)
                time.sleep(args.delay)
                outcome = _fetch_one_page(session, args, pool, page_num, None)
                outcomes.append(outcome)

                if outcome.state == "exhausted":
                    logger.info("The site offered no further page after page "
                                "%d — treating that as the end of the "
                                "listing.", page_num - 1)
                    stop_reason = "pagination_exhausted"
                    outcomes.pop()
                    break
                if outcome.state == "no_turnover":
                    stop_reason = "next_page_throttled"
                    outcomes.pop()
                    break
                if not outcome.ok:
                    stop_reason = ("page_load_timeout" if outcome.load_failed
                                   else f"blocked_{outcome.blocked_by}")
                    blocked = outcome.blocked_by is not None
                    break

                fresh_count = sum(1 for p in outcome.products
                                  if p.sku is None or p.sku not in seen_keys)
                seen_keys.update(p.sku for p in outcome.products
                                 if p.sku is not None)
                if not fresh_count:
                    logger.info("Page %d added no rows not already seen — "
                                "treating that as the end of the listing.",
                                page_num)
                    stop_reason = "no_new_products"
                    break
    finally:
        session.close()
        bridge.close()

    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key="sku")
        if len(fresh) < len(oc.products):
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    total_available = next((o.total_available for o in outcomes
                            if o.total_available is not None), None)
    total_is_floor = next((o.total_is_floor for o in outcomes
                           if o.total_available is not None), False)

    if args.mode == "listing" and all_rows:
        counts = [(o.page_num, len(o.products)) for o in outcomes if o.ok]
        fullest = max((n for _, n in counts), default=0)
        last_page = max((p for p, _ in counts), default=0)
        thin = [(p, n) for p, n in counts
                if fullest and n < THIN_PAGE_SHARE * fullest and p != last_page]
        if thin:
            logger.warning("Page(s) %s came back much thinner than the "
                           "fullest page (%d rows).",
                           ", ".join(str(p) for p, _ in thin), fullest)
        gaps = {o.page_num: o.gap for o in outcomes if o.gap}
        if gaps:
            logger.warning(
                "The site's own counter says %d card(s) never loaded across "
                "page(s) %s.", sum(gaps.values()),
                ", ".join(str(p) for p in sorted(gaps)))
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

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=source_of(final_url),
                      start_url=args.url, final_url=final_url, extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="Vrbo property scraper (pyppeteer edition)")
    p.add_argument("--url", default=None,
                   help="A Vrbo URL: a search grid (/search?destination=...) "
                        "or one property with --mode property. Required, "
                        "unless VRBO_URL is set in the environment or .env.")
    p.add_argument("--mode", choices=["listing", "property"], default="listing",
                   help="listing (default) or property.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with.")
    p.add_argument("--pages", type=int, default=1,
                   help=f"Number of listing pages to walk (default 1, cap "
                        f"{PAGE_CAP}). Each page past the first is reached by "
                        f"PRESSING the site's own next button.")
    p.add_argument("--delay", type=float, default=5.0,
                   help="Delay between pages, seconds (default 5.0).")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for family compatibility and REFUSED above "
                        "1: a Vrbo listing has no per-page address.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3).")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="vrbo_products", help="Output file prefix")
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Use the browser at PATH instead of pyppeteer's own "
                        "Chromium. THE flag that decides whether this engine "
                        "works: measured on the same address seconds apart, a "
                        "bundled Chromium was answered HTTP 429 and a real "
                        "Chrome HTTP 200 with the full grid. e.g. "
                        "'/Applications/Google Chrome.app/Contents/MacOS/"
                        "Google Chrome' or /usr/bin/google-chrome.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL. Credentials are sent over CDP "
                        "(page.authenticate), never on the command line.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line. Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES),
                   default="per-run",
                   help="per-run (default). per-page cannot be honoured "
                        "mid-listing here.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="Retries from other exits when a page is refused.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default). Expedia's handler picks its "
                        "vendor per request and the measured pick was "
                        "DataDome, which this repo has no solver for.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score (0.3, 0.7 or 0.9).")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to a running browser over CDP, e.g. "
                        "ws://user:pass@host:port. pyppeteer authenticates on "
                        "the WebSocket upgrade, so the Scraping Browser API "
                        "endpoint works from this engine.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
    p.add_argument("--headful", dest="headless", action="store_false",
                   default=False,
                   help="Run with a real browser window. THE DEFAULT here.")
    p.add_argument("--headless", dest="headless", action="store_true",
                   help="Run headless. Not measured to work on this site.")
    args = p.parse_args()
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and VRBO_URL is not set in the environment "
                "or in .env.")
    why = unsupported_reason(args.url)
    if why:
        p.error(why)
    kind = listing_kind(args.url)
    if args.mode == "property" and kind != "property":
        p.error(f"--mode property expects a property URL; {args.url!r} is a "
                f"{kind} page.")
    if args.mode == "listing" and kind == "property":
        p.error(f"{args.url!r} is a single property page. Use --mode "
                f"property for it, or pass a /search?destination=... URL.")
    if args.mode == "property" and args.pages != 1:
        logger.warning("--pages %d is ignored in --mode property.", args.pages)
        args.pages = 1
    if not args.chromium_path and not args.cdp_endpoint:
        # Said BEFORE the run rather than after a blocked page, because the
        # answer is a flag rather than a proxy and a reader who meets exit 3
        # first will go looking for the wrong thing.
        logger.warning(
            "No --chromium-path: this run will launch pyppeteer's own "
            "Chromium, which this site was measured REFUSING with HTTP 429 "
            "while serving a real Chrome from the same address seconds "
            "apart. Expect exit 3. Pass --chromium-path pointing at an "
            "installed Chrome, or use playwright_scraper.py.")
    if args.mode == "listing" and "startDate=" not in (args.url or ""):
        logger.warning(
            "This search carries no dates, so the site will quote each "
            "property its OWN cheapest one-night stay, and those prices are "
            "NOT comparable to each other. Add startDate and endDate for "
            "price monitoring, and read the stay_dates column either way.")
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
