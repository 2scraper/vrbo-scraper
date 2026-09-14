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
- **454 offline checks** over fixtures cut from real captures, each proved to
  parse identically to its untrimmed original.
- A daily canary that needs no secret, and reports an access condition as a
  warning rather than painting the badge red for something that is nobody's
  bug.

### Measured, and worth knowing before you run it

- **Real Chrome is required.** Same residential exit, seconds apart: `curl`
  429, Playwright's bundled Chromium 429, real Chrome **200** with the full
  grid. Every engine, the Dockerfile and the requirements files now say so;
  `playwright install chrome`, not `chromium`.
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

### Not verified

- The **Scraping Browser API** and **Scraper API** data paths. Both answered
  HTTP 401 because the 2captcha key available at the time returned
  `ERROR_KEY_DOES_NOT_EXIST`. Their error handling *was* verified: credentials
  are masked in the message and the run exits 5. The README says so rather
  than claiming a measurement that was not taken.

[Unreleased]: https://github.com/2scraper/vrbo-scraper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/2scraper/vrbo-scraper/releases/tag/v0.1.0
