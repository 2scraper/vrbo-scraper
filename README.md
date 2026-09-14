# vrbo-scraper

[![release](https://img.shields.io/github/v/release/2scraper/vrbo-scraper?sort=semver)](https://github.com/2scraper/vrbo-scraper/releases)
[![tests](https://github.com/2scraper/vrbo-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/vrbo-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/vrbo-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/vrbo-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%20%E2%80%93%203.12-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20pyppeteer-lightgrey)](#engines)
[![runs without an account](https://img.shields.io/badge/runs%20without-an%20account-brightgreen)](#do-you-need-any-of-the-paid-products)

Scrapes **Vrbo** property listings — a search grid, or one property's own
page — into JSON or CSV with a stable column schema, a run-metadata sidecar,
and exit codes that tell "blocked" from "empty" from "partial".

Works on Vrbo's five storefronts, which are five different catalogues:
`vrbo.com`, `fewo-direkt.de`, `abritel.fr`, `bookabach.co.nz`,
`stayz.com.au`.

> **Read this first if you only read one thing.**
> The thing that decides whether Vrbo answers you is **which browser binary
> you drive**, not which IP you come from. Measured 2026-09-14, same
> residential address, seconds apart:
>
> | client | result |
> |---|---|
> | `curl` with a Chrome user-agent | **HTTP 429**, "Bot or Not?" |
> | Playwright's **bundled Chromium**, real window | **HTTP 429**, "Bot or Not?" |
> | Playwright driving **real Chrome** (`channel="chrome"`) | **HTTP 200**, 899 KB, full grid |
>
> So: `playwright install chrome`. No proxy substitutes for it.

---

## Install and run

```bash
git clone https://github.com/2scraper/vrbo-scraper && cd vrbo-scraper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chrome            # NOT chromium — see the box above

python3 playwright_scraper.py \
  --url "https://www.vrbo.com/search?destination=Orlando,%20Florida,%20United%20States%20of%20America" \
  --pages 2 --out orlando
```

That writes `orlando.json`, `orlando.csv` and `orlando.meta.json`.

**Install exactly one engine.** The three engines' pins are mutually
unsatisfiable (`playwright` and `pyppeteer` disagree on `pyee`; `pyppeteer`
and `selenium` on `urllib3`). Use a virtualenv per engine if you need more
than one.

---

## The four things that will surprise you

### 1. A dateless search prices every property on a different night

With no dates in the URL, Vrbo quotes each property **its own cheapest
one-night stay**. Three cards in one measured load:

```
Quiet Oasis in Lovely Neighborhood   $172   for 1 night   Sep 16 - Sep 17
The Coach House                      $295   for 1 night   Sep 24 - Sep 25
The Point Hotel & Suites Orlando      $81   for 1 night   Sep 28 - Sep 29
```

Those prices are **not comparable to each other**, and the same row will
appear to change price between two runs when all that moved was the date.
The `stay_dates` column carries the window verbatim so you can see it, and
the run warns about it at startup.

**For price monitoring, pin the stay:**

```bash
--url "https://www.vrbo.com/search?destination=...&startDate=2026-11-14&endDate=2026-11-16&adults=2"
```

Then `stay_dates` is **null on every row** — measured, 50 of 50 — because the
card no longer needs to state a window you already asked for. A null there is
the signal that your prices *are* comparable. `diff_runs.py` knows this: a
price move that comes with a moved `stay_dates` is reported as
`stay_changed`, not `changed`, and `--fail-on-change` ignores it.

### 2. Pagination is a button, and the obvious URL tricks silently lie

There is no `link[rel=next]`, no `a[rel=next]`, no `<link rel=canonical>` and
no `hreflang` set anywhere on a listing page. Page 2 is
`button[data-stid="next-button"]`, which fires a GraphQL POST and leaves the
address bar untouched.

Worse, the conventions you would reach for do not fail — they return page 1:

| tried | result |
|---|---|
| `&startIndex=50` | HTTP 200, counter still `1 - 50 of 300+`, same cards |
| `&page=2` | HTTP 200, counter still `1 - 50 of 300+`, same cards |

A scraper built on either would find no new listings, conclude the catalogue
was exhausted, and report a **complete** run holding a sixth of it. This one
presses the site's own button instead, strictly sequentially, and **refuses
`--concurrency` above 1** with that as the reason. Run several searches in
parallel instead, one process each.

### 3. The page turn is rate limited separately from the page

The data behind a next-press comes over `POST /graphql`, and that endpoint
has its own limiter. Measured: the search page kept answering **HTTP 200**
while every `/graphql` POST behind a press came back **HTTP 429**
(`{"error":"Too Many Requests","message":"Provisioned request rate has been
exceeded"}`) and the site's own client gave up.

When that happens the run reports **`partial`, exit 6** — never "the listing
ended" — and says how many 429s it counted. `--delay` (default **5.0s**,
higher than the family default) is the cheap lever; `--proxy-file` is the one
that scales.

### 4. The results list is an inner scroller, and the page body never scrolls

`document.body.scrollHeight === window.innerHeight` on every capture. The
grid lives in `.scrollable-result-section`. Measured: scrolling the *window*
12 times changed nothing (18 cards before, 18 after); scrolling the
*container* reached **50 of 50 in two rounds**. First paint varies with the
split-view layout — 3 to 18 cards on the same URL and viewport — so almost
every row on a page comes from the scroll.

---

## How you know you got everything

The listing states its own size: `1 - 50 of 300+` in the pagination control.
So completeness here is **arithmetic, not a threshold** — if the page says it
holds items 1 to 50 and the run merged 40 rows, ten cards never loaded. That
is reported per page, recorded in the sidecar as `cards_missing`, and
**downgrades the run to `partial`**, because a consumer reading
`status: complete` beside a 31-card hole would take it for a delisting.

The German storefront writes the same counter as `1–50 von >300` — en dash,
floor marker on the other side — which is why it is parsed as "the integers
in that text".

---

## Output

One row per property. 26 columns; the first 14 are this repo family's shared
prefix, in the family's order, so a consumer written against a sibling repo
reads them unchanged.

| column | notes |
|---|---|
| `source` | which storefront — **not constant**, and not decorative |
| `sku` | the property id from the URL path |
| `expedia_property_id` | Expedia's own id. A **different number** from `sku` on a `/{id}` card (path `2430840` vs `expediaPropertyId=70477069`) |
| `price`, `currency` | currency from the page's own ISO code, falling back to the host — never a defaulted `"USD"` |
| `rating`, `rating_scale` | Vrbo rates out of **TEN**. The scale is its own column because the rest of this family publishes five-point ratings under the same name |
| `review_count` | `1,299 reviews` and `1.614 bewertungen` are 1299 and 1614 |
| `property_type`, `bedrooms`, `beds`, `property_summary` | the summary line split positionally; the raw line is kept beside it |
| `location_note` | usually a neighbourhood, sometimes `Kissimmee, 16.3 mi from Orlando` or `10 Min. Fahrt zum Strand`; the street address in `--mode property` |
| `badges` | `Premier Host` and whatever joins it |
| `amenities` | see the caveats below |
| `stay_dates`, `price_note` | verbatim; see surprise #1 |
| `price_source` | `card` / `card-a11y` / `detail` / `detail-a11y` |
| `page`, `position` | the pair; `position` restarts per page |

`sample_output.json` and `sample_output.csv` are cut from a real run
(Orlando, 2026-11-14 → 2026-11-16, 2026-09-14).

**Four family columns are deliberately absent**, each with the measurement
written down in `output_writer.py`: `original_price` and `discount_pct`
(**0** strike nodes across 218 cards, 6 captures and 4 storefronts — this
site has no discount chain on a card), `brand` (a property has no
manufacturer) and `in_stock` (a search result is bookable for the dates
quoted, so a `True` would be an inference dressed as a reading).

### Exit codes

`0` ok · `1` crash · `2` bad usage · `3` blocked · `4` zero results ·
`5` remote API error · `6` partial

A run that finds nothing **writes nothing**, so a failure cannot overwrite
last night's good output. `--allow-empty` is the opt-out.

---

## Traps that look like bugs

* **`image_url` is null on most rows.** 41 of 50 cards carry no `<img>`
  element at all — the gallery is not mounted below the fold. A null here is
  an unmounted gallery, not a property without photos.
* **`amenities` is empty on whole runs.** Present on 18 of 18 cards in one
  capture and 0 of 50 in another of the same URL minutes later. It is an A/B
  variant of the card, not a parsing failure.
* **Vrbo search returns hotels and aparthotels**, not only whole-home
  rentals — "The Point Hotel & Suites Orlando", `Aparthotel · 1 bedroom ·
  2 beds`. That is the site's inventory, not a wrong URL.
* **A card's `latLong` query parameter is the SEARCH centre**, identical on
  every card, not the property's own position. It is stripped and not
  recorded; the property page has the real address.
* **Vrbo silently ignores filter parameters it does not recognise.** A search
  with `price_max=1&minBedrooms=10` came back with 18 normal cards and
  `1 - 50 of 300+`. Use the site's own filter UI and copy the resulting URL.
* **`homeaway.com` redirects to `vrbo.com`** and serves nothing of its own.
  The scraper refuses it *with that reason* rather than claiming it is not a
  Vrbo site.

---

## Engines

| engine | notes |
|---|---|
| `playwright_scraper.py` | **Recommended.** Needs `--browser-channel chrome` (the default) — `playwright install chrome` |
| `selenium_scraper.py` | Drives the Chrome you already have, so it gets the right browser for free. **Cannot authenticate a proxy** (`--proxy-server` has nowhere to put a password) and **cannot use an authenticated CDP endpoint** (`debuggerAddress` is a bare `host:port`) |
| `puppeteer_scraper.py` | pyppeteer is effectively unmaintained. It downloads its **own Chromium**, which this site refuses, so `--chromium-path /path/to/chrome` is required in practice. It warns before the run rather than after a blocked page |
| `scraper_api_client.py` | One HTTP request per page via the 2Captcha Scraper API, no local browser. **Measured on this site**: HTTP 200, 3.2 MB, and **3 of 50 cards** at $0.0005 — see below |

All three browser engines produce the same rows. Measured on three live runs
of the same Orlando search on 2026-09-14: 50 rows each, and on the 38
properties all three runs happened to share, **every substantive column
agreed** — title, price, currency, rating, rating_scale, review_count,
property_type, bedrooms, beds, location_note, stay_dates,
expedia_property_id, price_note, sku, url, source. The only differences were
`scraped_at`, `position` (a live personalised search reorders between runs),
`image_url` and `badges` (both viewport- and A/B-dependent, as above).

Engine flag differences are asserted in both directions by the test suite, so
closing one needs a README edit rather than a quiet patch:

* `--browser-channel` — Playwright only
* `--chromium-path` — pyppeteer only
* `--cdp-connect-timeout` — Playwright and pyppeteer only (Selenium cannot
  use an authenticated CDP endpoint at all)
* `--locale`, `--fingerprint`, `--fp-tags`, `--fp-country` — Playwright and
  Selenium only

---

## Do you need any of the paid products?

**No.** An ordinary local Chrome on an ordinary residential connection reads
this site fine: every measurement in this README was taken that way, with no
key and no proxy.

What the 2Captcha products buy here, behind one key
([2captcha.com](https://2captcha.com)):

* **Proxies** — **the one that works here**, measured 2026-09-14: a 2Captcha
  residential exit (`region-be`) driving a real local Chrome returned
  **50/50 cards at 100% price coverage, `status: complete`, and no challenge
  of any kind**. Note what it is and is not for: a proxy does **not** get the
  wrong browser past the front door, it spreads the `/graphql` rate limit
  that stops a deep multi-page run.
* **The Scraping Browser API** (`--cdp-endpoint`) — a remote browser, so you
  do not run one. **Measured refused by Vrbo on 2026-09-14** — see below
  before spending anything on it. Selenium cannot reach it at all (see
  above).
* **Fingerprints** (`--fingerprint`) — a consistent device identity.
* **Captcha solving** — see the honest limits below.

### The Scraper API gets the top of the page, not the page

Measured 2026-09-14 against a live Orlando search, one request, **$0.0005**:

```
HTTP 200, 3,219,172 bytes of HTML
3 of 50 cards parsed — every column the card publishes, fully populated
```

Three is not a failure, it is the **first paint**. This path renders but
cannot scroll, and on this site almost every row comes from scrolling an
inner container. It also comes back **without the pagination counter**, so
the completeness oracle the browser engines rely on is not available: the
client cannot tell you how much it missed.

So use it to see what is at the top of a search cheaply — a work list, an
availability spot-check, "is this property still listed" — and use a browser
engine when you want the page. Pass the card selector or you will get a shell
with nothing in it:

```bash
python3 scraper_api_client.py --url "https://www.vrbo.com/search?destination=..." \
  --wait-element '[data-stid="lodging-card-responsive"]'
```

### The exit does not change the storefront

Measured on the same run: a **Belgian** residential exit fetching
`vrbo.com` still got `vrbo.com`, still priced in **USD**, and produced rows
identical in shape to a local run. Vrbo does not geo-redirect by exit
address — the storefront is the hostname, which is why there is no
`--country` flag anywhere in this repo.

### `--fingerprint` is verified end to end

Measured 2026-09-14: a fresh fetch from the Fingerprint API, and the
fingerprint actually applied — user agent, `locale`, `timezone_id`
(`America/New_York` for a US fingerprint), viewport and screen all set on the
browser context, plus the patch script. A live run with it returned 50/50
cards at 100% price coverage.

Pass **one** OS-family tag (`--fp-tags Windows`). A list is rejected by the
API with HTTP 400 — `Windows,Chrome,Desktop` was this repo family's default
for months and made `--fingerprint` fail on every invocation in four repos at
once.

### The Scraping Browser API connects — and Vrbo refuses it

Measured 2026-09-14 with a live `country-us` endpoint. The client side is
fine; the site is not:

```
connected over CDP in ~3s
Captcha.setAutoSolve enabled
page 1  -> HTTP 429, "Bot or Not?", 116,035 bytes
        retried twice through the same path — 429 again, both times
        whichChallenge: datadome-challenge
exit 3
```

Tried on **five exit countries** with the same profile — `us`, `de`, `gb`,
`nl`, `ca` — and all five answered HTTP 429 with a ~116 KB `Bot or Not?`
page. So it is the exit POOL that Expedia has scored, not any one country.

So **this path does not currently get you into Vrbo.** The remote browser is
a real Chrome, which is the thing this site cares about most — but its exit
address is refused, and Expedia's handler picks DataDome, which neither this
repo nor the endpoint's own auto-solve can answer. The engine behaved
correctly throughout: it named the vendor, spent nothing on a solve it could
not deliver, and exited 3 rather than writing an empty file over good data.

That may change — an exit pool is not a constant — so the path is kept and
documented rather than removed. But do not buy it expecting it to solve
access to this site today. **A residential `--proxy` is the paid product
that addresses the actual constraint here** (the `/graphql` rate limit on
page turns).

### Why there is no DataDome solver here

2Captcha *does* sell one (`DataDomeSliderTask`), so the obvious question is
why this repo does not call it. The answer is that on this site it has
nothing to solve:

* **The path that works never sees a challenge.** A residential proxy with a
  real local Chrome returned 50/50 cards and no challenge at all.
* **The path that sees one cannot use the answer.** A DataDome solution is
  an **IP-bound cookie** — it is only valid from the proxy passed in the
  task. The Scraping Browser leaves from 2Captcha's own exit, not from your
  proxy, so a solved cookie cannot be matched to the browser presenting it.

If Vrbo ever starts challenging residential exits, that calculation changes
and a solver becomes worth adding. It is not worth adding for a challenge
that only appears where its answer cannot be used.

> **Still not verified: a solve of any kind.** The key path into the solver
> is live (`getBalance` answers) and the gating is tested offline, but a
> solve is only ever attempted when Expedia's handler picks reCAPTCHA — and
> across every run here, local, proxied and remote, it picked DataDome.

### Captchas: what this repo can and cannot solve

Vrbo's refusal is Expedia's own **"Bot or Not?"** handler (HTTP 429, app
`captcha-pwa`, page id `wildcard-challenge-handler`). It is a
**multiplexer**: its page config names which vendor it picked *this time*,
as data rather than markup —

```
"whichChallenge": "datadome-challenge"
"siteKey": …            (reCAPTCHA v2)
"recaptchaV3Key": …
"turnstileSiteKey": …
"arkoseClientApiUrl": …
"powComplexity": 20     (a proof-of-work variant)
```

This repo solves **reCAPTCHA v2/v3 and nothing else**. So a challenge is only
offered to the solver when `whichChallenge` names reCAPTCHA; every other pick
— including DataDome, which is what the measured refusal was — is reported as
**blocked**, and **no solve is attempted and nothing is charged**.

Note also that `akamai` is *not* usable as a block marker here. Vrbo is
fronted by Akamai Bot Manager and every good page loads its sensor script, so
that string matches an 899 KB page holding the full grid.

---

## Configuration

Credentials go in `.env` beside the scripts, never on a command line — a
secret in `argv` is readable by anything that can run `ps` and lands in shell
history.

```bash
cp .env.example .env
python3 env_config.py     # prints what was picked up, WITHOUT secrets
```

Precedence, highest first: an explicit flag → an exported environment
variable → `.env` → the default. Anything still carrying `{...}` braces is
treated as unset, so a copied example is never sent to an API as if it were a
key.

Variables: `TWOCAPTCHA_KEY`, `VRBO_CDP_ENDPOINT`, `VRBO_PROXY`, `VRBO_URL`.

---

## Comparing two runs

```bash
python3 diff_runs.py --old monday.json --new tuesday.json
```

It refuses a pair it cannot honestly compare: a `partial` run, two different
`--mode`s, or **two different storefronts** — they are different catalogues
in different currencies, so the diff would be all noise.

---

## Tests

```bash
python3 smoke_test.py     # or: pytest
```

Over 450 checks (473 on 2026-09-14). Offline, no network, and it passes with
no engine library installed — the skips are reported, and CI fails on an
unexpected one. Fixtures are cut from
real captures by `make_fixtures.py`, which proves each one parses
*identically* to its untrimmed original, column for column.

`TROUBLESHOOTING.md` covers what to do when a column comes back empty.

---

## Licence and scope

MIT. This reads pages Vrbo serves to an anonymous visitor. It does not log
in, does not touch a booking flow, and does not attempt to defeat a challenge
it cannot legitimately solve. Check Vrbo's terms and your own jurisdiction
before pointing it at anything at volume, and use `--delay`.
