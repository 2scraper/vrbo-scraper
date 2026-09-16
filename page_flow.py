"""page_flow.py — what to do with the page Vrbo just gave us.

Vrbo answers a request five ways, and four of them want a different response,
which is why this module exists rather than the same triage being written
three times inside three engines and drifting apart (§1):

    content    the grid is in the document
    empty      a search that genuinely matched nothing. No grid, and — unlike
               a sibling repo's site — no backfill of suggestions either, so
               an empty answer here really is empty
    shell      served, built out of the site's own assets, grid not there
               yet. Wants a WAIT and a SCROLL, not a refetch
    challenge  Expedia's Bot-or-Not handler rendered something this repo can
               actually pay to have solved
    blocked    HTTP 429 and that same handler, having picked a vendor this
               repo cannot solve — which is the normal case here

The policy lives in `STATE_POLICY` as DATA, so an engine cannot quietly
disagree with its twins about whether a page is worth retrying or worth
paying for.

Everything here is pure or driven through small callables, so each engine
passes its own driver's primitives and keeps its browser plumbing to itself:

    count(selector) -> int              how many elements match
    scroll_results() -> None            scroll the RESULTS container down
    results_height() -> Optional[int]   that container's scroll height
    press_next() -> bool                press the site's next-page button
    first_card_href() -> Optional[str]  the first card's link
    sleep(ms) -> None                   wait

No JavaScript crosses that boundary in either direction (§1): Selenium's
`execute_script` takes a function BODY with an explicit `return` where
Playwright and pyppeteer take `() => expr`, so this module names the
OPERATION and each engine spells it in its own driver's dialect.

This module DOES have a scroll loop, and that is the opposite of a sibling
repo where the same measurement said not to
------------------------------------------------------------------------
On this site the scroll is the only way most of the grid is ever seen, and
the shape of it is unusual enough to be worth stating twice:

  * The page body NEVER scrolls. `document.body.scrollHeight ===
    window.innerHeight === 900` on every capture. §8's "scroll to
    `document.body.scrollHeight`" moves nothing at all here — measured, 12
    window scrolls, 18 cards before and 18 after.
  * The results are in an INNER scroller, `.scrollable-result-section`.
    Scrolling THAT reached 50 of 50 cards in two rounds and then held still.
  * The first paint is small and varies with the split-view layout: 18 cards
    on one load and 3 on another, same URL, same viewport.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from product_parser import (SELECTORS, NEXT_PAGE_SELECTOR, PAGE_CAP,
                            challenge_vendor, detect_block_marker,
                            detect_bot_challenge, detect_page_state,
                            is_challenge_page, listing_kind, paginates_by_url,
                            results_range, served_by_vrbo, sku_from_url,
                            strip_tracking)

logger = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
READY_SELECTOR_LISTING = SELECTORS["item_card"]
# A property page paints its title first and its price block last, so the
# title alone would resolve on a page with no price on it. The price block is
# the thing a row is built from.
READY_SELECTOR_PROPERTY = SELECTORS["detail_price"]

# Above 1, per §5: waiting for a single match resolves on an unrelated node
# long before the grid paints. Two rather than the measured first-paint
# minimum of three, so a genuine two-result listing is not made to spend the
# whole timeout — and `min_matches` clamps it further when the site's own
# counter says the page holds fewer.
MIN_CARD_MATCHES = 2
MIN_CARD_MATCHES_PROPERTY = 1

# Generous against a measured first paint of 2-4s, because a residential
# exit and a cold cache are both slower than a laptop on a home connection,
# and the cost of waiting too long is latency where the cost of waiting too
# little is a run holding three cards out of fifty.
CONTENT_TIMEOUT_MS = 25_000
CONTENT_TIMEOUT_MS_PROPERTY = 20_000

_READY = {"listing": READY_SELECTOR_LISTING, "property": READY_SELECTOR_PROPERTY}
_MIN = {"listing": MIN_CARD_MATCHES, "property": MIN_CARD_MATCHES_PROPERTY}


def ready_selector(mode: str) -> str:
    return _READY.get(mode, READY_SELECTOR_LISTING)


def min_matches(mode: str, expected: Optional[int] = None) -> int:
    """How many matches mean "painted".

    `expected` is what the site's own counter says this page holds, and
    passing it is what keeps a short last page from timing out: a listing
    whose final page holds one property can never reach two.
    """
    floor = _MIN.get(mode, MIN_CARD_MATCHES)
    if expected is None or expected <= 0:
        return floor
    return max(1, min(floor, expected))


def content_timeout_ms(mode: str) -> int:
    return CONTENT_TIMEOUT_MS_PROPERTY if mode == "property" else CONTENT_TIMEOUT_MS


def expected_cards(html: Optional[str]) -> Optional[int]:
    """How many cards this page's own counter says it holds."""
    return results_range(html or "").expected_on_page


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, minimum: int, timeout_ms: int,
                   poll_ms: int = 250) -> int:
    """Poll `selector` until `minimum` elements match, or the budget runs out.

    Polls a COUNT rather than waiting on an evaluated string. Playwright's
    `wait_for_function` hands the browser a string to evaluate, which a site
    whose CSP lacks `unsafe-eval` refuses outright — it took a sibling repo's
    run down with `EvalError` and exit 1 on that site's most obvious URL
    (§18). Vrbo's own CSP was not audited page-kind by page-kind here, which
    is exactly why the question is not being asked: a count poll is a CDP
    call under every CSP and spells the same in all three drivers.

    Returns the last count seen, so a caller can tell "painted" from "timed
    out with three of them".
    """
    waited = 0
    seen = count(selector)
    while seen < minimum and waited < timeout_ms:
        sleep(poll_ms)
        waited += poll_ms
        seen = count(selector)
    if seen < minimum:
        logger.info("readiness wait ended at %d/%d matches for %s after %dms",
                    seen, minimum, selector, waited)
    return seen


# ---------------------------------------------------------------------------
# The scroll, which on this site is where most of the data comes from
# ---------------------------------------------------------------------------
# Three stable rounds, not one. §8's rule, and the measurement behind it
# here: scrolling the results container took the count 18 -> 50 in the first
# round and the container's height 5,099 -> 10,232 in the second, then both
# held still for twenty-three more rounds. A loop that stopped at the first
# unchanged round would have stopped at 18.
SCROLL_STABLE_ROUNDS = 3
# Enough for a 50-card page at the measured two productive rounds, with a
# wide margin for a slow exit, and a hard stop so a page that grows forever
# cannot hang a run.
SCROLL_MAX_ROUNDS = 20
# How many stable rounds to tolerate while the page's own counter says there
# are still cards to come. Earned on the first live run of this engine: page
# 2 settled at 19 cards against a counter that said 50, because the next
# batch was in flight behind a throttled `/graphql` POST and three quiet
# rounds went by while it was. A pause is not an ending when the site has
# told us how many there are — so the stable-round heuristic is the
# termination condition only for a page that published no counter, and below
# a known target the loop is twice as patient before giving up.
SCROLL_STABLE_ROUNDS_BELOW_TARGET = 6
# The next batch takes longer to arrive than a single pause (§8).
SCROLL_PAUSE_MS = 1_600


def scroll_until_settled(count: Callable[[str], int],
                         scroll_results: Callable[[], None],
                         results_height: Callable[[], Optional[int]],
                         sleep: Callable[[int], None],
                         selector: str = READY_SELECTOR_LISTING,
                         target: Optional[int] = None,
                         max_rounds: int = SCROLL_MAX_ROUNDS) -> int:
    """Scroll the results container until the grid stops growing.

    Requires the card count AND the container height to hold still for
    `SCROLL_STABLE_ROUNDS` consecutive rounds — the count alone is not
    enough, because a batch can be in flight with the count unchanged and the
    height already growing.

    `target` is what the site's own counter says the page holds. Reaching it
    ends the loop immediately, which saves four idle rounds on every page;
    NOT reaching it does not end anything early, because the gap is reported
    rather than chased (§8: the rank-gap arithmetic).

    Returns the final card count.
    """
    seen = count(selector)
    height = results_height()
    stable = 0
    for _ in range(max_rounds):
        if target is not None and seen >= target:
            break
        scroll_results()
        sleep(SCROLL_PAUSE_MS)
        now, now_height = count(selector), results_height()
        if now == seen and now_height == height:
            stable += 1
            # More patience while the site's own counter says cards are
            # still owed. See SCROLL_STABLE_ROUNDS_BELOW_TARGET.
            allowed = (SCROLL_STABLE_ROUNDS_BELOW_TARGET
                       if target is not None and now < target
                       else SCROLL_STABLE_ROUNDS)
            if stable >= allowed:
                break
        else:
            stable = 0
        seen, height = now, now_height
    return seen


# ---------------------------------------------------------------------------
# Classification and the policy that follows from it
# ---------------------------------------------------------------------------
def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "") -> str:
    """Which of the five states this response is.

    `status` is positional and comes SECOND, matching `detect_page_state`.
    Getting that wrong is not a style question: a sibling repo shipped two of
    three engines calling this as `classify(html, url=...)`, both crashed on
    their first fetch, and nothing short of a live run or a signature-binding
    check saw it (§17). This repo's smoke suite binds every shared-module
    call in every engine for that reason.
    """
    if html is None:
        return "blocked"
    return detect_page_state(html, status, url)


# The retry/solve/blocked decision as DATA rather than as three copies of an
# if-chain in three engines (§1).
#
#   parse    is there anything on this page worth writing down?
#   retry    would fetching it again, later or from a different exit,
#            plausibly help?
#   solve    is there something to pay a solver for?
#   blocked  does this count towards exit 3?
STATE_POLICY: Dict[str, Dict[str, bool]] = {
    "content":   {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # Nothing to parse and nothing to retry: the site was asked a question
    # and answered it. Vrbo does NOT backfill an empty search with
    # suggestions, so unlike one sibling repo this state is safe to treat as
    # simply empty rather than as a trap.
    "empty":     {"parse": False, "retry": False, "solve": False, "blocked": False},
    # Served and still painting. Wants the readiness wait and the scroll,
    # not another fetch: refetching a shell buys another shell (§18). Parsed
    # because by the time an engine asks, the wait has already run.
    "shell":     {"parse": True,  "retry": False, "solve": False, "blocked": False},
    "challenge": {"parse": False, "retry": True,  "solve": True,  "blocked": False},
    "blocked":   {"parse": False, "retry": True,  "solve": False, "blocked": True},
}


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["parse"]


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["blocked"]


def is_unpainted(state: str, html: Optional[str]) -> bool:
    """Whether this page is served but has not painted its grid yet."""
    if state != "shell":
        return False
    return served_by_vrbo(html or "")


# Retrying a block DOES help on this site, which is the opposite of a sibling
# repo and is measured rather than assumed. The refusal here is a RATE
# response — HTTP 429, `Provisioned request rate has been exceeded` on the
# GraphQL side — and the same URL that answered 429 answered 200 with the
# full grid a few minutes later from the same address and the same browser.
#
# So the budget is non-zero, and it is larger with a pool than without,
# because a different exit carries a different rate bucket. The engines read
# these constants rather than computing their own budget — a policy constant
# nothing consults is the same defect as dead code (§17).
RETRY_ON_BLOCKED = True
BLOCK_RETRIES_WITHOUT_POOL = 2
BLOCK_RETRIES_WITH_POOL = 3

# One solve per page. Detection is broad on purpose, but a second solve on
# the same page has never been the answer to the first one failing.
SOLVES_PER_PAGE = 1


def block_advice(html: Optional[str], headless: bool, has_pool: bool) -> str:
    """What a reader should actually DO about this block.

    Exists because the honest first answer on this site is almost never "get
    a better proxy" — it is "use a real Chrome" — and a message that says so
    saves an afternoon and a proxy bill.
    """
    vendor = challenge_vendor(html or "")
    marker = detect_block_marker(html or "") or "HTTP 429"
    lead = f"blocked ({marker})"
    if vendor and not detect_bot_challenge(html or ""):
        lead = (f"blocked ({marker}) — Expedia's challenge handler picked "
                f"{vendor!r} this time, which this repo has no solver for, so "
                f"no solve was attempted and nothing was charged")
    hints = [
        "this site reads the CLIENT before the address: a bundled Chromium "
        "was answered 429 and a real Chrome 200 from the same exit seconds "
        "apart, so run the Playwright engine with its default "
        "channel=chrome before reaching for anything else",
    ]
    if headless:
        hints.append("and with a real window — --headful is the default here "
                     "for that reason")
    if not has_pool:
        hints.append("the 429 is a RATE response and it clears: the same URL "
                     "was served minutes later from the same address. Slow "
                     "down with --delay, or spread the load with --proxy-file")
    else:
        hints.append("with a pool in play, raise --delay before raising "
                     "--concurrency: N workers from N addresses still means "
                     "N times the request rate at the site")
    return lead + ". " + "; ".join(hints) + "."


# ---------------------------------------------------------------------------
# Pagination — a button, not an address
# ---------------------------------------------------------------------------
# There is deliberately no `page_url()` and no `next_page_candidates()` in
# this module, because on this site there is no page-2 address to build.
# `&startIndex=50` and `&page=2` were both tried with a real browser and both
# answered HTTP 200 with the counter still reading "1 - 50 of 300+" and the
# same first cards — they do not fail, they silently return page 1 (§18). A
# run built on either would add no new sku, call the listing exhausted and
# report COMPLETE holding a sixth of the catalogue.
#
# What works is pressing the site's own button, which fires a GraphQL POST
# and leaves `window.location` untouched. Measured: the counter moved from
# "1 - 50 of 300+" to "51 - 100 of 300+" with zero title overlap against
# page 1.

# How long to wait for a next-press to take effect. Generous, and it has to
# be: the press fires a `/graphql` POST that was observed answering 429
# ("Provisioned request rate has been exceeded") on the first attempt, with
# the site's own client retrying and succeeding on the second. A budget
# tuned to the happy path would report the listing exhausted every time that
# happened.
NEXT_PAGE_TIMEOUT_MS = 60_000
NEXT_PAGE_POLL_MS = 1_000


def has_next_page(count: Callable[[str], int]) -> bool:
    """Whether the site is offering a next page at all.

    The weakest of the termination signals and therefore the second one
    asked: a missing button is a property of the DOM, while "this page added
    no sku we had not already seen" is a property of the catalogue (§7).
    """
    return count(NEXT_PAGE_SELECTOR) > 0


# The three ways a page turn can end, and keeping them apart is the whole
# reason this returns a string rather than a bool.
#
# The first version of this function returned False for both of the last two,
# and the engine mapped False to `pagination_exhausted` — which is in
# COMPLETE_STOP_REASONS. So a run that was being RATE LIMITED reported status
# "complete" holding page 1. That is precisely the silent-success failure
# this family exists to prevent (§7), and it was caught by running the thing
# rather than by reading it (§15).
ADVANCED = "advanced"
NO_BUTTON = "no_button"          # the listing genuinely ran out
NO_TURNOVER = "no_turnover"      # pressed, and the grid never came back


def advance_to_next_page(press_next: Callable[[], bool],
                         first_card_href: Callable[[], Optional[str]],
                         count: Callable[[str], int],
                         sleep: Callable[[int], None],
                         timeout_ms: int = NEXT_PAGE_TIMEOUT_MS) -> str:
    """Press next and wait for the grid to actually turn over.

    Returns ADVANCED, NO_BUTTON or NO_TURNOVER — see above for why the last
    two must not be the same answer.

    Readiness here is the FIRST CARD'S SKU CHANGING, and the two more obvious
    signals were both tried and both are wrong:

      * the URL — never changes, by design;
      * the counter element — is destroyed and rebuilt during the
        transition, so a poll on it reads `None` mid-flight and a loop
        waiting for its text to change gives up on a page that was about to
        arrive. Measured: a 45-second wait on the counter reported "never
        changed" on a press that had in fact worked.

    The card count going briefly to zero is NORMAL during the transition:
    the results container is rebuilt, collapsing to its client height with
    `scrollTop` back at 0 (measured). What is NOT normal is it staying that
    way, and on this site the reason is almost always that the `/graphql`
    POST behind the turn was answered 429.
    """
    before = first_card_href()
    before_sku = sku_from_url(strip_tracking(before or "")) if before else None
    if not press_next():
        return NO_BUTTON
    waited = 0
    while waited < timeout_ms:
        sleep(NEXT_PAGE_POLL_MS)
        waited += NEXT_PAGE_POLL_MS
        if count(READY_SELECTOR_LISTING) <= 0:
            continue
        now = first_card_href()
        now_sku = sku_from_url(strip_tracking(now or "")) if now else None
        if now_sku and now_sku != before_sku:
            logger.info("next page arrived after %dms", waited)
            return ADVANCED
    logger.info("next page never turned over within %dms", timeout_ms)
    return NO_TURNOVER


def page_cap_reached(page_num: int) -> bool:
    return page_num >= PAGE_CAP


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------
def page_gap(html: Optional[str], parsed: int) -> Optional[int]:
    """How many cards the page's own counter says are missing, or None.

    None rather than 0 when the counter is absent: an unknown gap is not a
    gap of zero, and the two must not read the same in a sidecar. Where the
    counter IS there this is arithmetic rather than a threshold — the page
    states it holds items 1 to 50, so 40 rows means ten cards never loaded
    (§8). One measured page-2 run had exactly that.
    """
    expected = expected_cards(html)
    if expected is None:
        return None
    return max(0, expected - parsed)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def concurrency_limit(url: str) -> Optional[int]:
    """The highest `--concurrency` this URL can honestly support.

    Always 1. Page 5 of a listing that only exists behind four button presses
    cannot be handed to a worker (§18).
    """
    return 1


def concurrency_refusal(url: str) -> Optional[str]:
    """Why concurrency above 1 is refused for this URL.

    Refused WITH the reason rather than silently running one worker, which
    would look like the flag did something.
    """
    kind = listing_kind(url)
    if kind == "property":
        return ("a property page is a single page; --concurrency above 1 has "
                "nothing to fetch")
    if not paginates_by_url(url):
        return ("a Vrbo listing has no per-page address — page N exists only "
                "behind N-1 presses of the site's own next button, so pages "
                "cannot be fetched independently and --concurrency above 1 "
                "has nothing to hand a second worker. Run several searches "
                "in parallel instead, one process each")
    return None
