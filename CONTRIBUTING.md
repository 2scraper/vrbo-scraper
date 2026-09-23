# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Vrbo changing its markup is the normal way this stops working, and it
has its own issue template. The detail that saves the most time is WHICH
anchor broke, because on this site there is no structured data on a listing
page to fall back on — zero `application/ld+json`, zero `__NEXT_DATA__`, and
an `__APOLLO_STATE__` holding only a banner, because the grid arrives over
client-side POSTs to `/graphql` — so the DOM is not the primary path by
preference, it is the only one. Every anchor is one of Vrbo's own
`data-stid` attributes, never a `uitk-…` design-system class.

1. **The card.** `[data-stid="lodging-card-responsive"]`, and the property
   link inside it, `a[data-stid="open-product-information"]`. If either
   moves the run reports 0 rows and exit 4, which is loud.
2. **The price container, which has two spellings.**
   `data-stid="product-price-summary"` on vrbo.com and
   `data-test-id="price-summary"` on the local storefronts. A third
   spelling shows up as a null price column on ONE storefront while every
   other column looks fine — so say which host you ran.
3. **The scroll container.** `.scrollable-result-section`. The page body
   never scrolls; if this moves, first paint (3 to 18 cards) is all you get
   and the sidecar reports `cards_missing`.
4. **Pagination.** `[data-stid="next-button"]` and the counter in
   `[data-stid="pagination-navigation"]` (`1 - 50 of 300+`). The counter is
   what the completeness arithmetic reads.

A property page (`--mode property`) carries two JSON-LD blocks, a
`BreadcrumbList` (read for `category`) and an `FAQPage`; its price, address
and reviews still come from the DOM.

`--dump-html PATH` writes the exact bytes the parser was given, on success as
well as failure, and a run that finds nothing writes a dump and a screenshot
next to the output on its own.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once —
   including its WARNING branch, which is what runs when a bare GitHub
   runner's datacentre address is refused and no `VRBO_PROXY` secret is set.
   Unlike most sibling repos in this family, this canary needs no secret to
   try: it installs real Chrome and runs it headful. What decides access
   here is how the exit ADDRESS is scored, and whether a runner's datacentre
   address is served has not been measured, which is exactly why a block there is
   a warning rather than a failure — until you set `VRBO_PROXY`, after which
   it is a failure, because then it means something.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions with inline HTML/JSON fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

The properties below exist because they were once absent or are easy to get
wrong. Tests pin them, so a PR that breaks one will fail rather than
silently regress:

- **A dateless search prices every property on a different night.**
  `stay_dates` carries the window verbatim, and `diff_runs.py` buckets a
  price move that comes with a moved window as `stay_changed`, not
  `changed`. With `startDate`/`endDate` in the URL it is null on every row.
- **Completeness is arithmetic.** The pagination counter states which items
  a page holds, so fewer rows than that is `cards_missing` and downgrades
  the run to `partial` — never a threshold.
- **A throttled page turn is `partial`, never "the listing ended".** The
  HTML keeps answering 200 while the `/graphql` POST behind the next button
  answers 429, and `stop_reason` says `next_page_throttled`.
- **There is no per-page address.** `&page=2` and `&startIndex=50` answer
  200 with page 1, so `--concurrency` above 1 is refused with that reason.
- **Two different ids.** `sku` is the property id from the URL path;
  `expedia_property_id` is Expedia's own, a DIFFERENT number on a `/{id}`
  card. Do not merge them.
- **`rating` is out of ten**, which is why `rating_scale` is its own column.
- **The refusal names its vendor as data.** Expedia's "Bot or Not?" handler
  (HTTP 429) states `whichChallenge`; only a reCAPTCHA pick is offered to
  the solver, and every other pick is reported as blocked with nothing
  charged. Block detection is also positive: a served page is recognised by
  the site's own asset hosts (`travel-assets.com`, `media.vrbo.com`),
  because Chromium's network-error page carries the site's hostname in its
  title.
- **A run that finds nothing writes nothing.** It must not replace a good output
  file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows, `5` remote API error, `6` partial. A
  pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.** One page
  past the end of a listing has no lots, and a no-results search has none of
  its own; both are correct answers to the question that was asked.
  `page_flow.STATE_POLICY` holds that for all three engines so they cannot
  disagree about it.
- **A challenge marker is only consulted for a state already counted as
  blocked**, and a marker that matches every page of the site is not a
  marker at all. That is why `akamai` is NOT in this repo's marker set: Vrbo
  is fronted by Akamai Bot Manager and every good page loads its sensor
  script, so the string matches an 899 KB page holding the full grid. What
  identifies a refusal here is the handler itself (`Bot or Not?`,
  `captcha-pwa`, `wildcard-challenge-handler`).
- **A sku already written by an earlier page of the same run is dropped, not
  duplicated.** See `dedupe_by_key` in `output_writer.py`.

There is also a naming check: certain phrases are banned repo-wide and the suite
fails naming them. If it trips, read the message — the phrase is wrong for a
reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha classifier
and the CLI contract against inline fixtures. If yours genuinely needs
vrbo.com, say in the PR what you ran, which URL and page kind, from
which exit, and what you got — including the price and image coverage
percentages the run prints, `cards_missing`, and the `stop_reason` from the
sidecar. Access here depends on how the exit address is scored and is
noisy, so a single exit 3 is not a finding on its own. Row counts differ by
search, by storefront and by how far the scroll got, so a bare "worked for
me" is not reproducible.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification: in a sibling repo (tokopedia-scraper) the first live run
of the pyppeteer engine crashed on its FIRST fetch on a signature mismatch
that four separate offline checks and 400 green assertions had not caught.

Do not add anything that submits a booking, enquiry or sign-in form. This
project deliberately never does.

## Scope

This repo scrapes **public pages** on Vrbo and its sibling storefronts:
search listings and property pages, exactly as an anonymous visitor is served them.
Out of scope: anything behind a login, anything that submits a form, and
anything that defeats a protection rather than passing it the way an ordinary
browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
