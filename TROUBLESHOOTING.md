# Troubleshooting

Read this before filing an issue. Half of what looks broken on this site is
the site working as designed, and the other half has a one-line answer.

---

## "It returns HTTP 429 / `Bot or Not?` / exit 3"

**Check your browser before you check your IP.** This is the single most
common cause and it has nothing to do with your address:

```bash
playwright install chrome        # NOT chromium
```

Measured 2026-09-14, same residential exit, seconds apart — bundled Chromium
got 429, real Chrome got 200 and the full grid. The Playwright engine
defaults to `--browser-channel chrome`; if that channel is not installed it
says so and falls back, and the fallback is what gets refused.

Per engine:

| engine | what to do |
|---|---|
| `playwright_scraper.py` | `playwright install chrome`. `--browser-channel msedge` also works |
| `selenium_scraper.py` | nothing — it drives the Chrome you already have |
| `puppeteer_scraper.py` | `--chromium-path /path/to/chrome`. Its own Chromium is the refused build |

Only once you have the right browser is a block worth reading as a block. The
run then tells you which vendor the challenge handler picked, e.g.
`blocked (datadome-challenge)`, and saves the page to
`<out>_page1_debug.html` and `.png`.

**Can I solve it?** Only if `whichChallenge` in that dump says reCAPTCHA.
This repo implements reCAPTCHA v2/v3 and nothing else, so a DataDome, Arkose,
Turnstile or proof-of-work pick is reported as blocked and **nothing is
charged**. That is deliberate — paying for a solve that cannot be delivered
is worse than reporting the block.

---

## "The run stops after page 1 and says `partial` / exit 6"

Look at `stop_reason` in `<out>.meta.json`.

**`next_page_throttled`** — you are being rate limited on the page turn, not
on the page. The data behind a next-press comes over `POST /graphql`, which
has its own limiter: the HTML keeps answering 200 while the turn is refused
with `{"error":"Too Many Requests","message":"Provisioned request rate has
been exceeded"}`. The log says how many 429s it counted.

In order of cost:

1. `--delay 15` (the default is 5)
2. wait a few minutes — this clears on its own
3. `--proxy-file` to spread the load across exits

It is reported as partial rather than complete **on purpose**. Reporting a
throttled turn as "the listing ended" would give you a `complete` run holding
one page of six, which is the exact failure this repo is built to avoid.

**`cards_missing`** — the run got every page it asked for, but a page
delivered fewer cards than its own counter said it holds. See the next
section.

---

## "`cards_missing` in the sidecar / fewer rows than I expected"

The listing states its own size in the pagination control (`1 - 50 of 300+`),
so this is arithmetic rather than a guess: the page said items 1 to 50 and the
parse got 40, so ten cards never loaded.

Almost always the scroll did not finish. First paint on this site is 3 to 18
cards and the rest arrive by scrolling an **inner** container
(`.scrollable-result-section`) — the page body never scrolls at all. If the
batch behind a scroll is throttled, the grid stops growing early.

Same remedies as above: slow down, or use a proxy pool. Re-run with
`--dump-html` and open the snapshot to confirm the cards really were absent
rather than unparsed.

---

## "Every price changed overnight and nothing else did"

Check `stay_dates`. If it is populated, your search had **no dates in the
URL**, and Vrbo then quotes every property its own cheapest one-night stay —
a different night per property, and a different night tomorrow.

```bash
--url "https://www.vrbo.com/search?destination=...&startDate=2026-11-14&endDate=2026-11-16&adults=2"
```

With dates pinned, `stay_dates` is **null on every row** (measured 50 of 50)
and the prices are comparable. `diff_runs.py` already knows: a price move
that comes with a moved `stay_dates` is bucketed as `stay_changed`, not
`changed`, and `--fail-on-change` ignores it.

---

## "`image_url` is null on most rows"

Expected. 41 of 50 cards carry no `<img>` element at all — the gallery is not
mounted below the fold. The column is populated for the cards that were on
screen and recognised positively by the media host, so a placeholder can
never fill it with something that is not an image.

In `--mode property` it comes from `og:image` and is reliably present.

---

## "`amenities` is empty on every row"

Also expected, and not a parsing failure: the amenity strip is an A/B variant
of the card. Measured 18 of 18 cards in one capture and 0 of 50 in another of
the same URL minutes later.

---

## "`bedrooms` and `beds` are null on a German or French run"

The summary line on a hotel or aparthotel card is just the type
(`Aparthotel`) with no bedroom count after it. The columns are split
positionally from that line, so there is nothing to split. `property_summary`
holds the raw line either way — read it to confirm.

---

## "`--concurrency 4` says it is refused"

It is, and the message says why: a Vrbo listing has **no per-page address**.
Page 5 exists only behind four sequential presses of the site's own button,
so there is nothing to hand a second worker. `&page=2` and `&startIndex=50`
do not work — they answer 200 and return page 1, which is worse than failing.

Run several searches in parallel instead, one process each.

---

## "A column is 100% populated and wrong"

Re-run with `--dump-html` — it writes the snapshot **on success too**, which
is the only way to tell a parsing bug from a too-early snapshot. Then:

```bash
python3 - <<'EOF'
from product_parser import parse_products
html = open("dump.page1", encoding="utf-8").read()
for row in parse_products(html, "https://www.vrbo.com/search?destination=X")[:3]:
    print(row)
EOF
```

If the markup has genuinely moved, the fix is in `product_parser.py` and
nowhere else. Note that this site spells the price container **two ways** —
`data-stid="product-price-summary"` on vrbo.com and
`data-test-id="price-summary"` on the local brands — so a third spelling is a
plausible cause of a null price column on one storefront only.

---

## "The tests pass but a live run is broken"

That is the normal shape of a site-side change, and it is why `canary.yml`
exists. To refresh the offline fixtures against the current site:

```bash
python3 playwright_scraper.py --url "..." --pages 2 --dump-html capture
# move the dumps into ../captures/ with the names make_fixtures.py expects
python3 make_fixtures.py
python3 smoke_test.py
```

`make_fixtures.py` refuses to write a fixture that does not parse identically
to its untrimmed original, so a bad trim fails loudly rather than pinning the
wrong behaviour.

---

## "`--cdp-endpoint` says `profile_locked` and never clears"

A Scraping Browser profile allows **one live connection**, so the obvious
reading is that another run holds it. Two profiles measured here say that is
not always what is happening.

What was observed, 2026-09-14, against a live endpoint:

* the credential was fine — the endpoint's HTTP sibling answered `200` with
  `Chrome/151.0.7922.174`;
* on the first profile, the WebSocket upgrade **hung for 121 seconds** and
  then the server hung up. Afterwards it answered `500 profile_locked` on
  both WebSocket and HTTP, and had **not cleared forty minutes later**;
* a second, fresh profile answered `500 profile_locked` on its **very first
  connection attempt**, and was still locked after four minutes of no
  requests at all.

So: waiting does not clear it, and it is not necessarily another run of this
tool. **Nothing on the client side frees a profile in that state** — use a
different `pid`, or reset the profile from the 2Captcha dashboard.

### Do NOT poll the HTTP endpoint to check

An earlier version of this section suggested a "non-destructive" `GET
/json/version` to see whether a profile is free. Treat that as unsafe: on
both profiles the first such call answered `200` and everything afterwards
was locked. Whether the GET itself claims the profile was not established —
but it is consistent with what was seen, and polling it is exactly what was
being done to the profile that never recovered. If you want to know whether a
profile is free, try the connection you actually want and read the error.

### What `--cdp-connect-timeout` is and is not for

The default is **150s**, up from the 30s this repo family shipped, because
the server's own give-up point was measured at 121s and a client that quits
first quits while the server is still working.

It is **not** a cure for `profile_locked`. The second profile above locked
instantly, with no timed-out connect anywhere in its history.

---

## "Which storefront should I use?"

The one that has the inventory. They are five different catalogues:

| host | language | currency |
|---|---|---|
| `vrbo.com` | en | USD |
| `fewo-direkt.de` | de | EUR |
| `abritel.fr` | fr | EUR |
| `bookabach.co.nz` | en | NZD |
| `stayz.com.au` | en | AUD |

A Berlin rental is on `fewo-direkt.de` and nowhere else. `diff_runs.py`
refuses to compare two of them for exactly that reason.

`homeaway.com` redirects to `vrbo.com` and serves nothing of its own; the
scraper refuses it with that as the message.
