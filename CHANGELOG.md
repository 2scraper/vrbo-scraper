# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. Read a PATCH as "fixes", not as "every flag is frozen":
where a default has to change because the old one was measured not to work,
that is a fix, and the release notes lead with it.

## [Unreleased]

> **Behaviour change.** When Expedia's handler picks **DataDome**, a run with
> `TWOCAPTCHA_KEY` and a `--proxy` that the browser itself exits through now
> **buys at most one DataDome solve per page** (`DataDomeSliderTask`,
> 2Captcha; about $0.00145 each when measured on 2026-09-14). It then installs
> the cookie and re-loads the page. The page is `challenge` now, not
> `blocked`; the exit code when it is not cleared is still **3**. Nothing is
> bought:
> - over `--cdp-endpoint`, or without a proxy;
> - by Selenium with a proxy login;
> - for a page reached by a button press;
> - for `t=bv`;
> - when the page clears by itself within 15 s.
>
> To keep the old behaviour, leave `TWOCAPTCHA_KEY` unset.
>
> Measured on 2026-10-23 over seven canary walks:
> - 6 solves were bought, and 2 were accepted, both through the widget
>   step. Those two runs went on to 3 pages and 150 rows.
> - The cookie-and-re-load path was accepted 0 times out of 4.
> - No-solve control: the first-page job met DataDome in all seven runs and
>   never cleared by itself.
>
> See the README "Live result".

> **Behaviour change.** A page turn that gets no grid back (the site's
> `/graphql` answered HTTP 429, with no `Retry-After`) is now retried
> **once** in all three engines, after a random 30-60 s pause, in the same
> browser session and exit. A run that used to end `next_page_throttled`
> (exit 6) at once can now take up to about two minutes longer at that
> page, and goes on to the next page when the retry succeeds.
> `--throttle-wait-seconds 0` restores the old behaviour.

> **Behaviour change.** On a 2Captcha residential gateway
> (`*.proxy.2captcha.com`, `*.proxy.rucaptcha.com`), a proxy login with no
> `-session-` segment now gets one random `-session-<id>-sessTime-30`
> appended for the run, so every page leaves from one exit.
> `--proxy-session-minutes` / `VRBO_PROXY_SESSION_MINUTES` sets the minutes
> (0 to 120); `0` sends the login unchanged. A login that already names a
> session is used as given.

> **Behaviour change.** A run through a proxy now makes one pre-check
> request before starting a browser. A proxy that answers **HTTP 407** or
> **402** ends the run with **exit 5** and
> `<out>.latest_attempt.meta.json` (`status: failed`, `stop_reason:
> proxy_auth_failed` / `proxy_balance_exhausted`), instead of timing out on
> every retry. Runs without a proxy or over `--cdp-endpoint` are unchanged.
>
> **Behaviour change.** A run through a local proxy now **stops at 10 MB of
> proxy traffic** by default (`--proxy-budget-mb`, `0` = no cap). It keeps
> its rows and exits **6** (**5** with none) with `stop_reason:
> proxy_budget_exceeded`, and it always writes
> `<out>.latest_attempt.meta.json`. Media is loaded by default on this site,
> so a long walk through a proxy reaches the cap sooner.

### Added

- **A DataDome fallback through 2Captcha, in all three engines.**
  - **Settle first.** The challenge page gets up to 15 s to clear by itself
    or mount its slider.
  - **Solve.** The slider's iframe URL, the browser's own
    `navigator.userAgent` and the browser's current proxy exit go into a
    `DataDomeSliderTask`. That exit includes the sticky `-session-` login.
    The task is polled for up to 180 s, and only after the page's one-solve
    allowance has been claimed.
  - **Through the widget.** The answer is reported the way the slider does
    on a pass: a message posted from inside the slider's iframe. The site's
    own tag then installs the cookie and session, and its handler page
    validates and leaves. The engine gives this 20 s. Playwright and
    Selenium can post from the iframe; pyppeteer cannot reach a
    cross-origin iframe and skips this step.
  - **Apply.** If the page is still the challenge, the returned `datadome`
    cookie is installed with the challenge page's own cookie domain, path,
    `Secure` and `SameSite`, read back, and the page is re-loaded.
  - **Validate.** If the handler page comes back, the engine waits for it
    to clear again. It then makes the handler's own validate call
    (`POST /botOrNot/validate` with the page's `exchangeHandshake`).
  - The widget message and the validate call are reproduced from the
    site's served tag and challenge bundle. Neither has been observed on
    the wire.
  - **Selenium** can do this only through a proxy without a login.
  - **Sidecar.** It gains `captcha_kind`, `captcha_solves` (tasks created),
    `captcha_solved` (answers returned), `captcha_accepted` (answers after
    which the page was served) and `captcha_attempts` (one record per
    decision, with the reason nothing was bought).
    `<out>.latest_attempt.meta.json` is now also written for any run that met
    a captcha, with or without a proxy.
  - **Tests.** Offline checks drive a stub solver API on 127.0.0.1, a
    scripted browser and each engine's real fetch loop. An opt-in
    `DATADOME_BROWSER=1` group runs each engine's cookie, iframe, UA and
    validate operations in a real Chromium against a local stub site.
- **The canary's walk passes `TWOCAPTCHA_KEY`**, so a DataDome pick on its
  first page is solved through `VRBO_PROXY`. The job summary reports the
  captcha fields, and the attempt sidecar is uploaded.
- `--throttle-wait-seconds` in all three engines (default 45, `0` = no
  retry): one paced retry of a page turn that got no grid back. The pause is
  a random 2/3 to 4/3 of the value. The proxy budget is checked before the
  pause and again before the second press, so a spent budget never buys a
  retry. A grid that arrives during the pause counts as the turn and is not
  pressed again. The listing sidecar gains `throttle_retries` (retries made
  in the run). Measured on the 2026-10-22 canary walk (run 39858684619):
  pages 1 and 2 turned in 2 s, then the press for page 3 got one `/graphql`
  429 with no `Retry-After`, and the grid stayed empty for 60 s. Offline
  checks drive each engine's real page fetch with a stubbed grid: 429 then
  success reads the page, 429 twice ends exit 6 after exactly two presses,
  and a budget spent by the failed turn makes no second press.
- `--proxy-session-minutes` in all three engines, and
  `VRBO_PROXY_SESSION_MINUTES` in `.env.example`. The session id is drawn
  once per run, before the pre-check; with `--proxy-file`, each gateway line
  gets its own id after duplicates are dropped. The sidecar gains
  `proxy_session_pinned` (never the id). Selenium records `false`, because
  it cannot send a proxy login. Offline checks cover the login rewrite, the
  cases left alone, one login across the pre-check and later reads,
  rotation, flag and environment validation, and the sidecar field in each
  engine.
- `proxy_meter.py`: a per-run proxy traffic meter and budget in all three
  engines. It is active only through a local proxy.
  - Counting: Playwright sums `request.sizes()`. pyppeteer and Selenium count
    CDP `Network.loadingFinished.encodedDataLength`, plus each request's own
    size.
  - Over budget: Playwright aborts new requests in a route, pyppeteer and
    Selenium block every URL over CDP, and the run stops before its next
    navigation or page turn.
  - New sidecar fields `proxy_bytes`, `proxy_budget_bytes` and
    `proxy_budget_exceeded`, and a closing log line
    `Proxy traffic: X.X MB of Y MB budget`.
  - `--proxy-budget-mb`, and `VRBO_PROXY_BUDGET_MB` (in `.env.example`).
- CI only: both canary runs pass a `--proxy-budget-mb` (10 for the first
  page, 15 for the walk; see Changed). Each one writes its proxy traffic to
  the step summary, and fails with
  `::error::stopped: proxy budget N MB exceeded` when the cap was hit
  (`.github/proxy_traffic.py`).
- Offline checks:
  - The meter against known CDP and Playwright sizes.
  - An HTTP client through a stub proxy that counts every byte.
  - The sidecars and exit codes over budget.
  - Every engine driven past the budget.
  - The canary steps and their summary script.
  - An opt-in real-browser check (`PROXY_METER_BROWSER=1`), which compares
    each driver's count with a stub proxy's.

- `proxy_check.py`: the proxy pre-check (one CONNECT plus `HEAD /robots.txt`
  on www.vrbo.com, no third-party service) for all three engines.
  - On a 407, the message names `VRBO_PROXY` and says the gateway refused
    the credential (or the traffic balance is exhausted).
  - On a 402, it says the proxy traffic/balance is exhausted.
  - Neither message prints the credential.
  - A proxy that does not answer keeps the existing retry behaviour.
- `--block-media` (opt-in) aborts image, video/audio and font requests on
  all three engines. It is off by default here: with images blocked,
  `image_url` came back null on some rows. `--load-media` and
  `VRBO_LOAD_MEDIA` state the default explicitly.
- Offline checks:
  - A stub proxy that answers 407, 402 or 200, or nothing at all.
  - Each engine is driven to exit 5 on a 407 without starting a browser.
  - The attempt sidecar.
  - The route handler, the Fetch patterns and the blocked-URL list.
  - When each engine installs blocking.

### Changed

- CI only: the canary's three-page walk through `VRBO_PROXY` passes
  `--proxy-budget-mb 15`; the scraper's default stays 10. With
  `--block-media` the 2026-10-22 walk used 8.7 MB for pages 1 and 2 at 50 of
  50 cards each, so a full third page and a paced retry would not fit under
  10. The step summary already reported each run against the budget in its
  own sidecar (`proxy_budget_bytes`); its docstring said "of 10 MB" and now
  says so, and the canary checks cover a 15 MB run and a run with no cap.
- CI only: the whole canary, free and paid jobs alike, now runs weekly (Mondays 06:20 UTC) and on a manual dispatch; the daily schedule is removed.

### Fixed

- A results page that stops filling partway now gets the rest of its cards
  in all three engines. Below the last card the grid keeps loading
  placeholders (`data-stid="search-listing-placeholder"`). It fills them only
  when they scroll into view, and scrolling to the bottom again moves
  nothing. The 2026-10-20 and 10-21 canaries stopped this way: 6 placeholders
  under 13 cards on page 1 with no proxy, and under 19 cards on pages 2 and 3
  through a residential exit. Each run ended as `cards_missing`, exit 6.
  - When count and height hold still for three rounds with placeholders
    showing, the scraper scrolls the container to the top and back down a
    screen at a time. It does this at most three times per page.
  - Measured 2026-10-22 without a proxy: a 13-card stall held for eight
    rounds, and one nudge took it to 50 of 50.
  - Each page's `scroll` entry in the sidecar now records `nudges` and
    `placeholders_left`, and the warning says which case it was.
  - New sanitized fixture `LISTING_LAZY_TAIL`, cut from the stalled canary
    capture. There are offline checks for the stall, the bound, short pages,
    the walk and each engine's wiring.
- CI only: the canary's three-page walk through `VRBO_PROXY` passes
  `--block-media`. A full page loads about 100 images (1.8 MB measured), and
  the 2026-10-21 walk had already used 9.6 MB of its 10 MB cap with two short
  pages.
- The pyppeteer and Selenium proxy meter counts only `http://` / `https://` requests and responses that came over the network. Chrome's own loads (the `chrome://` new-tab page, `data:`, `blob:`), cache hits (`requestServedFromCache`, `fromDiskCache`, `fromPrefetchCache`, `fromServiceWorker`) and Chrome's error page count nothing. A sibling repo's Selenium run had counted about 2.8 MB of new-tab scripts while its proxy carried about 6 KB. The pyppeteer block sent once the budget is spent now reaches the page's own event loop even when the budget runs out on another thread.

## [0.3.0] — 2026-10-15

> **Behaviour changes:**
>
> - A run that obtained nothing now exits **5**, not 4, when the site served
>   a page without its results (its own "Sorry, we're having a problem on our
>   end" copy, or a grid that never painted) or when cards were on the page
>   and the parser read 0 rows. Exit 4 is kept for the site's own no-results
>   answer. With rows from earlier pages the same failure gives exit 6.
> - A challenge still on the page after the allowed solve and retries now
>   exits **3** (blocked). It used to fall through to the parser and exit 4.
> - `--pages 0`, `--concurrency 0` and negative `--delay`, `--retry-delay`,
>   `--block-retry-delay` and `--proxy-block-retries` are refused with a
>   usage error (exit 2) in all three engines, as are negative `--retries`
>   and `--retry-delay` in `scraper_api_client.py`.
> - Output files are replaced atomically. A new file gets the mode `open()`
>   would give it under your umask; an existing file keeps its own mode.
> - CSV cells that begin with `=`, `+`, `-`, `@`, a tab, CR or LF are
>   prefixed with `'` so a spreadsheet reads them as text. The JSON output is
>   unchanged; the sidecar records the count as `csv_cells_escaped`.

### Fixed

- **A paid solve could be bought several times for one page.** The solve
  before classification was not counted, and it ran again on every block
  retry, so a page that stayed a challenge bought one solve per attempt
  (measured with a stubbed solver: 4 in each engine against a limit of 1).
  One `page_flow.SolveBudget` per page now covers both call sites in all
  three engines and is checked before the purchase; a retry or a rotation
  does not reset it.
- **A site error page or an unsolved challenge read as an empty catalogue.**
  Parsing now runs only for states the policy says are worth parsing, the
  page is re-classified after the readiness wait, and a served page with no
  rows that is not the site's own no-results answer is recorded as
  `served_without_results` or `parser_found_nothing` (see the exit codes
  above). A sanitized fixture cut from the 2026-10-07 canary capture covers
  the error page.
- **The navigation's HTTP status now reaches the classifier** in the
  Playwright and pyppeteer engines, so a refusal with no challenge body is
  not read as a page still painting. Selenium exposes no status.
- **A failed write could truncate good output.** JSON, CSV and the
  `.meta.json` sidecar are written to a temporary file beside the target,
  flushed, synced and renamed over it; on an error the previous file is
  untouched and no temporary file is left.
- **A card rendered without a price carried a currency.** A currency now
  needs a price beside it, on listing and property rows ("This property does
  not allow 2-night stays for those dates", from a real capture).
- **The credential scan passed with 0 files read** when the clone lived
  under a directory named `venv` or `.venv`: it tested the absolute path.
  It now tests the path relative to the repository, treats an empty scan as
  a failure, and reads committed `.json`, `.csv` and `.html` files too.
- **The canary was green without testing anything.** Every "success" for at
  least 20 days was a block (exit 3) or a partial run whose page turn was
  refused (exit 6), and its pinned stay dates were drifting towards the
  past. It now has a `first-page` job that needs no secret and fails unless
  page 1 yields rows that pass `.github/canary_check.py`, and a `pagination`
  job that walks three pages through the `VRBO_PROXY` secret and fails,
  naming the secret, when it is absent. Stay dates are computed from the run
  date.
- Comments that described another site's behaviour (a discovery hub, an
  Indonesian no-results string, a connection reset) were replaced with this
  site's, and `.gitignore` now covers captures by kind (`*.html`, `*.htm`,
  `*.pageN`, `live/`, `dumps/`).
- `tests.yml` runs the `--help` and sample checks from `.github/ci_checks.py`
  only, instead of a second inline copy of each; `diff_runs.py` joined the
  `--help` list.
- README and CONTRIBUTING describe exit 5 as "content not obtained", which
  is what it covers, and the storefront count matches the host table (five).
- Shipped files no longer cite private notes or section numbers; each rule
  is stated where it applies, and the suite fails if such a citation
  returns.

## [0.2.0] — 2026-10-08

> **Behaviour change:** `--retries 0` is now refused with a usage error
> (exit 2). It used to send no request at all and report exit 3 (blocked),
> so a script passing it was reading a refusal the site never gave. Use
> `--retries 1` for "no retry".

### Added

- **`--block-retry-delay SECONDS`** (all three engines). Wait before
  re-fetching a refused page, doubling per retry. Unset keeps the previous
  linear `retry-delay x attempt` wait, so existing runs behave identically.
  One `page_flow.block_pause` serves the three engines. The retry count is
  unchanged; there is no jitter, since one client has no herd.
- **`schema_version` in the `.meta.json` sidecar** (`1.0.0`), versioning the
  output contract rather than the package. `diff_runs.py` refuses two runs
  whose MAJOR differs.

### Fixed

- **`--retries 0` sent no request at all and reported exit 3.** Every engine
  loops `range(1, retries + 1)` around the navigation, so 0 meant zero
  navigations: the page stayed `about:blank` (39 bytes of empty DOM), the
  classifier called that `blocked`, and the run exited 3 with a 39-byte debug
  dump having never contacted the site. `--retries` now refuses anything
  below 1 (`page_flow.attempts_arg`), in all three engines.
- **The block advice and three help/log texts blamed the browser build.** They
  still said Vrbo "reads the CLIENT before the address" and that bundled
  Chromium is refused, which the README's own 12-of-12 interleaved
  measurement had already retracted. They now say the exit's score is noisy
  and the browser is not the first lever.

### CI

- **The canary asks for three pages, not two.** Two exercises the next-button
  walk once, which proves the button was found; three is what catches a
  cursor that does not advance, and is the family rule. `pages_completed` is
  already asserted against `PAGES_REQUESTED`, so the change makes the canary
  strictly stricter. `MIN_ROWS` is deliberately left at 60 — it was measured
  as a floor for a two-page run, and scaling it to three would be inventing a
  number no run has produced. It is simply a more conservative floor now, and
  the note in the workflow says to raise it only when a three-page run has
  been measured, naming that run.

- **Scraper API: `waitFor` is now sent as a JSON object.** It went out as a
  JSON-encoded string, following a docstring that said the API required one.
  Measured 2026-09-23 against `/tasks/sync`: the string form is refused with
  HTTP 422 ("params.waitFor must be an object") and still billed ($0.0005);
  the object form answers HTTP 200. Every `--wait-*` run was therefore
  a paid exit 5.
- **Scraper API: the target's HTTP status is read from `http_code`.** The
  client read `status`, which is the API's own verdict string (`"success"`),
  so the page classifier never saw a target 403/503 — a live rakuten run
  logged "Upstream page status success". `status` is now a fallback only
  when it is an integer.

### Docs — leftovers from the repos this one was copied from

Text describing another site as if it were Vrbo, removed or rewritten from
this repo's own README, TROUBLESHOOTING and parser:

- **`captcha_solver.py`** said no challenge had ever been observed here and
  that a block was a headless browser refused by Akamai with a 394-byte
  "Access Denied". Both were another site's. It now says what the README
  measured: Expedia's "Bot or Not?" handler (HTTP 429) picked DataDome on
  every recorded refusal, and there is deliberately no DataDome path. The
  "No DataDome solver" section the docstring pointed at now exists.
- **`CONTRIBUTING.md`**: the site-change anchors (a different site's
  `data-testid` grid and tile markers), the list of pinned properties
  (auction lots, reserve prices, bid counts, a 100-page cap) and the
  `akamai` marker note (which contradicted this repo's own measurement)
  are rewritten for Vrbo. Also corrected: the canary note that the browser
  build is the gate, and a "datacentre gets no response at all" claim.
- **Issue templates**: the bug-report hints and the site-change template
  described another site's payload, lots and locales; both now describe
  Vrbo's cards, storefronts and `stop_reason`s. Example commands use a Vrbo
  search URL.
- **`diff_runs.py`**: example filenames, a `sold`/`sold_is_floor` note for a
  column this repo does not have, and a docstring listing
  `original_price`/`discount_pct`/`in_stock` and JSON-LD provenance. The
  docstring now lists the tracked fields and the `stay_changed` bucket.
- **`.gitignore`** ignored another repo's default output prefix; it now
  ignores `vrbo_products.*`, the engines' real `--out` default.
- **`SECURITY.md`** named only Akamai as the protection, said the project
  has no releases (v0.1.0 exists) and pointed at a README section that does
  not exist.

## [0.1.0] — 2026-09-14

First release on this repo family's architecture. The previous contents of
this repository — three standalone scripts with their own flags, their own
output shapes and no shared schema — are **replaced**, not extended. Nothing
in the old CLI carries over.

### Added

- **Two modes.** `--mode listing` reads a `/search?destination=…` grid;
  `--mode property` reads one property page and adds the street address and
  the location breadcrumb that a card does not publish.
- **Five storefronts**, each verified with a live run: `vrbo.com`,
  `fewo-direkt.de`, `abritel.fr`, `bookabach.co.nz`, `stayz.com.au`. They are
  five different catalogues in four currencies, and `source` says which.
- **Three engines behind one row schema** — Playwright (recommended),
  Selenium and pyppeteer — plus `scraper_api_client.py` for a browserless
  fetch. Verified on three live runs of the same search: 50 rows each, and
  every substantive column agreed on the 38 properties all three shared.
- **A 26-column schema** with a run-metadata sidecar, family-standard exit
  codes (`0/1/2/3/4/5/6`), and JSON + CSV output. A run that finds nothing
  writes nothing, so a failure cannot overwrite a good result.
- **`diff_runs.py`** with a `stay_changed` bucket: a price move that comes
  with a moved stay is not a repricing, and `--fail-on-change` ignores it.
- **Over 450 offline checks** (473 on 2026-09-14) over fixtures cut from
  real captures, each proved to parse identically to its untrimmed original.
  Stated as a floor on purpose: an exact count goes stale the next time
  anyone adds a check, and the suite asserts the floor rather than the
  number.
- A daily canary that needs no secret, and reports an access condition as a
  warning rather than painting the badge red for something that is nobody's
  bug.

### Measured, and worth knowing before you run it

- **Access is scored per exit address, and it is noisy** — the same address
  serves some requests and challenges others minutes apart. Real Chrome is
  the default (`--browser-channel chrome`) because it is the more faithful
  client, NOT because the build is a gate: measured 2026-09-14, bundled
  Chromium and real Chrome interleaved on a clean address, six requests each,
  **12 of 12 served**.

  An earlier draft of this release led with the opposite claim — that the
  browser binary decided access — on the strength of three single requests
  (`curl` 429, bundled Chromium 429, real Chrome 200). One sample per arm on
  a process that serves roughly one request in three does not support it. The
  claim had been propagated into the README, both engine docstrings, two
  requirements files, the Dockerfile and the canary; all are corrected.
- **A dateless search prices every property on a different night**, so its
  prices are not comparable with each other. Pin `startDate`/`endDate`; the
  `stay_dates` column is null when you have.
- **Pagination is a button with no address.** `&page=2` and `&startIndex=50`
  answer HTTP 200 and return page 1, so a scraper built on either would
  report a *complete* run holding a sixth of the catalogue. `--concurrency`
  above 1 is refused with that as the reason.
- **The page turn is rate limited separately from the page.** A refused turn
  is reported as `partial` (exit 6), never as the end of the listing.
- **The listing states its own size** (`1 - 50 of 300+`), so a missing card is
  arithmetic rather than a threshold — and it downgrades the run to
  `partial`.

### Verified with a live 2Captcha key

- **Scraper API**: HTTP 200, 3.2 MB, **3 of 50 cards** at $0.0005 a request —
  the first paint, since this path renders but cannot scroll. Its response
  also carries no pagination counter, so it has no completeness oracle. Good
  for the top of a search, not for a page.
- **Fingerprint API**: a fresh fetch, and the fingerprint actually applied —
  user agent, locale, `timezone_id`, viewport and screen. A live run with
  `--fingerprint` returned 50/50 cards at 100% price coverage.

- **Residential proxy**: a 2Captcha `region-be` exit driving a real local
  Chrome returned **50/50 cards, 100% price coverage, `status: complete`**
  and no challenge of any kind. This is the paid product that helps on this
  site. Measured on the same run: a Belgian exit on `vrbo.com` still gets
  `vrbo.com` and USD, so the site does **not** geo-redirect by exit address —
  which is why there is no `--country` flag.
- **Scraping Browser API**: connects in ~3s and enables
  `Captcha.setAutoSolve` — and Vrbo then answers **HTTP 429** with a 116 KB
  `Bot or Not?` page on the first request and both retries
  (`whichChallenge: datadome-challenge`), so the run exits 3. The path works;
  the site refuses the exit. Documented rather than removed, because an exit
  pool is not a constant — but the README says plainly not to buy it for this
  site today. Tried on five exit countries (`us`, `de`, `gb`, `nl`, `ca`):
  all five refused, so it is the exit pool that is scored, not a geography.

- **DataDome**: 2Captcha solves it; applying the solution was NOT achieved.
  Two halves with different answers, and they are reported separately.
  Solving works — `DataDomeSliderTask` is the right type, five successes in
  nine attempts at $0.00145 and 38-68s each. Applying the returned cookie did
  not demonstrably grant access: 200 once and 429 twice, against a control
  run in which the very same exit served a clean 200 with **no solve and no
  cookie at all**. Access landed about one time in three whether or not a
  solution was applied, so no effect is visible above that noise. The README
  carries the full control table, the one defect found on our side (the
  solver's cookie string omits `Secure`/`SameSite`, which the page's own
  cookie has), and the untested hypothesis that the answer has to be driven
  through the widget rather than pasted behind it, since Expedia has its own
  `/botOrNot/validate` step on top.

### Not verified

- **This repo's own `--solve-captcha` path end to end.** It fires only when
  Expedia's handler picks reCAPTCHA, and it picked DataDome on every run
  here — local, proxied and remote. The DataDome work above went through a
  standalone script against the 2Captcha API, not through this repo.

[Unreleased]: https://github.com/2scraper/vrbo-scraper/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/2scraper/vrbo-scraper/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/2scraper/vrbo-scraper/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/2scraper/vrbo-scraper/releases/tag/v0.1.0
