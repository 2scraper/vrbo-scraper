# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. Read a PATCH as "fixes", not as "every flag is frozen":
where a default has to change because the old one was measured not to work,
that is a fix, and the release notes lead with it.

## [Unreleased]

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

[Unreleased]: https://github.com/2scraper/vrbo-scraper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/2scraper/vrbo-scraper/releases/tag/v0.1.0
