# VRBO Scraper — Open-Source Vacation Rental Data Extraction

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/node-18%2B-green?logo=node.js&logoColor=white" alt="Node 18+">
  <img src="https://img.shields.io/badge/license-MIT-brightgreen" alt="MIT License">
  <img src="https://img.shields.io/badge/CAPTCHA%20solving-2captcha.com-orange" alt="2captcha">
  <img src="https://img.shields.io/badge/proxies-2prx.com-blueviolet" alt="2prx">
</p>

Free, open-source scraper for **[vrbo.com](https://www.vrbo.com)** vacation rental listings. Extracts property titles, pricing, ratings, reviews, amenities, images, availability, and more — across **all VRBO categories** (houses, condos, cabins, villas, apartments, cottages, chalets, townhouses, studios).

Three implementations are included so you can pick the stack you're most comfortable with:

| Script | Stack | Best For |
|--------|-------|----------|
| `vrbo_scraper_playwright.py` | **Playwright** (Python) | **Recommended** — fastest, most reliable |
| `vrbo_scraper_selenium.py` | Selenium + undetected-chromedriver (Python) | Teams already using Selenium |
| `vrbo_scraper_puppeteer.js` | Puppeteer + stealth plugin (Node.js) | JavaScript / Node.js workflows |

---

## Features

- **Three-layer extraction** — API/XHR interception → embedded JSON parsing → adaptive DOM scraping
- **All VRBO property categories** — houses, condos, cabins, villas, apartments, and more
- **Search + detail scraping** — search results pages and individual property pages
- **CAPTCHA bypass** — automatic reCAPTCHA v2/v3, hCaptcha, and Cloudflare Turnstile solving via [2captcha.com](https://2captcha.com/?from=vrbo-scraper)
- **Proxy support** — rotate residential & datacenter proxies via [2prx.com](https://2prx.com/?from=vrbo-scraper)
- **Stealth mode** — fingerprint randomization, WebGL spoofing, navigator overrides
- **Human-like behavior** — random delays, mouse movements, natural scrolling patterns
- **Flexible output** — JSON or CSV
- **Pagination** — automatically follows result pages
- **Headed or headless** — debug visually or run in CI/CD

---

## How It Works — Three-Layer Extraction

VRBO is a heavily JavaScript-rendered SPA. Static CSS selectors break frequently. Our scraper uses a cascading three-layer extraction strategy for maximum resilience:

1. **API/XHR Interception** — Captures listing data directly from VRBO's internal GraphQL and REST API responses as the page loads. This yields the cleanest, most complete data.
2. **Embedded JSON Parsing** — Scans the page for `__NEXT_DATA__`, `application/ld+json`, and inline `<script>` blobs that contain serialized listing data.
3. **Adaptive DOM Scraping** — Falls back to DOM traversal: finds property links by URL pattern, walks up to card containers, and extracts data from visible text using regex.

All three strategies run on every page. Results are merged and deduplicated by property ID or URL.

**Debugging:** If the scraper returns 0 results, run with `--debug --headed` to inspect the page visually and save diagnostic screenshots.

---

## Extracted Data Fields

| Field | Description |
|-------|-------------|
| `title` | Property listing title |
| `property_id` | VRBO property identifier |
| `url` | Direct link to the listing |
| `price_per_night` | Nightly price (USD) |
| `price_text` | Full price string as displayed |
| `rating` | Guest rating (e.g., 4.8) |
| `reviews_count` | Total number of reviews |
| `bedrooms` | Number of bedrooms |
| `bathrooms` | Number of bathrooms |
| `sleeps` | Max guest capacity |
| `property_type` | House, Condo, Cabin, Villa, etc. |
| `image_url` | Main listing image |
| `description` | Full property description* |
| `amenities` | List of amenities* |
| `host` | Host/owner info* |
| `location` | Property address/area* |
| `house_rules` | House rules list* |
| `images` | All property images (up to 20)* |
| `scraped_at` | Timestamp of extraction |

*Fields marked with \* are available when using the `--details` flag (individual property pages).

---

## Quick Start

### Playwright (Recommended)

```bash
# Install dependencies
pip install playwright twocaptcha-python
playwright install chrome           # Real Chrome — NOT chromium!

# Basic search
python vrbo_scraper_playwright.py --destination "Orlando, FL"

# Full options (proxy recommended for reliable results)
python vrbo_scraper_playwright.py \
  --destination "Maui, Hawaii" \
  --checkin 2025-08-01 \
  --checkout 2025-08-07 \
  --max-pages 10 \
  --max-properties 100 \
  --details \
  --format csv \
  --output maui_rentals.csv \
  --proxy "http://user:pass@gate.2prx.com:8080" \
  --captcha-key "YOUR_2CAPTCHA_API_KEY"
```

> **Important:** VRBO uses Akamai Bot Manager which blocks headless Chromium.
> The scraper automatically uses real Chrome (`channel="chrome"`) and a warm-up phase.
> For the best success rate, add a residential proxy via `--proxy`.

### Selenium

```bash
pip install selenium undetected-chromedriver twocaptcha-python

python vrbo_scraper_selenium.py \
  --destination "Cancun, Mexico" \
  --max-pages 5 \
  --format json
```

### Puppeteer (Node.js)

```bash
npm install puppeteer puppeteer-extra puppeteer-extra-plugin-stealth 2captcha

node vrbo_scraper_puppeteer.js \
  --destination "Lake Tahoe, CA" \
  --max-pages 5 \
  --format json
```

---

## CLI Options

| Flag | Description | Default |
|------|-------------|---------|
| `--destination`, `-d` | Search location (required) | — |
| `--checkin` | Check-in date (`YYYY-MM-DD`) | — |
| `--checkout` | Check-out date (`YYYY-MM-DD`) | — |
| `--max-pages` | Maximum search result pages | `5` |
| `--max-properties` | Max properties to collect (`0` = no limit) | `0` |
| `--details` | Scrape individual property pages for full data | `false` |
| `--format` | Output format: `json` or `csv` | `json` |
| `--output`, `-o` | Output file path | auto-generated |
| `--proxy` | Proxy URL (`http://user:pass@host:port`) | — |
| `--captcha-key` | 2captcha.com API key | — |
| `--headed` | Run browser in visible mode | `false` |
| `--debug` | Save debug screenshots and extra logging | `false` |

---

## CAPTCHA Solving with 2captcha.com

VRBO may present CAPTCHAs during scraping. This scraper integrates with [2captcha.com](https://2captcha.com/?from=vrbo-scraper) to solve them automatically:

| CAPTCHA Type | Supported |
|---|---|
| reCAPTCHA v2 | ✅ |
| reCAPTCHA v3 | ✅ |
| hCaptcha | ✅ |
| Cloudflare Turnstile | ✅ |

**Setup:**

1. Sign up at [2captcha.com](https://2captcha.com/?from=vrbo-scraper)
2. Get your API key from the dashboard
3. Pass it with `--captcha-key YOUR_KEY`

Or set the environment variable:

```bash
export TWOCAPTCHA_API_KEY="your_key_here"
```

---

## Proxy Support with 2prx.com

For large-scale scraping, use rotating proxies from [2prx.com](https://2prx.com/?from=vrbo-scraper) to avoid rate limits and IP blocks:

```bash
python vrbo_scraper_playwright.py \
  --destination "Miami Beach, FL" \
  --proxy "http://user:pass@gate.2prx.com:8080"
```

**Why 2prx.com?**

- Residential & datacenter proxy pools
- Geo-targeting (US, EU, global)
- Automatic IP rotation
- High uptime and speed
- Pay-per-GB pricing

---

## Anti-Detect Browser

For the highest success rate on heavily protected pages, combine this scraper with the **[2captcha Anti-Detect Browser](https://2captcha.com/anti-detect-browser)**:

- Real browser fingerprints (Canvas, WebGL, AudioContext, fonts)
- Unique browser profiles per session
- Integrated proxy management
- Cookie and session persistence

[Learn more →](https://2captcha.com/anti-detect-browser)

---

## Output Examples

### JSON

```json
[
  {
    "title": "Oceanfront Paradise — 3BR Condo with Pool",
    "property_id": "1234567",
    "url": "https://www.vrbo.com/1234567",
    "price_per_night": 289,
    "price_text": "$289 per night",
    "rating": 4.8,
    "reviews_count": 142,
    "bedrooms": 3,
    "bathrooms": 2,
    "sleeps": 8,
    "property_type": "Condo",
    "image_url": "https://images.vrbo.com/...",
    "scraped_at": "2025-07-15T10:30:00Z",
    "source": "vrbo.com"
  }
]
```

### CSV

```
title,property_id,url,price_per_night,rating,reviews_count,bedrooms,bathrooms,sleeps,property_type
"Oceanfront Paradise — 3BR Condo with Pool",1234567,https://www.vrbo.com/1234567,289,4.8,142,3,2,8,Condo
```

---

## Project Structure

```
vrbo-scraper/
├── vrbo_scraper_playwright.py   # Primary scraper (Playwright)
├── vrbo_scraper_selenium.py     # Selenium alternative
├── vrbo_scraper_puppeteer.js    # Puppeteer / Node.js alternative
├── requirements.txt             # Python dependencies
├── package.json                 # Node.js dependencies
├── README.md                    # This file
└── LANDING_PAGE.md              # Product landing page content
```

---

## Requirements

### Python (Playwright / Selenium)

- Python 3.10+
- See `requirements.txt`

### Node.js (Puppeteer)

- Node.js 18+
- See `package.json`

---

## Legal Disclaimer

This tool is provided for **educational and research purposes**. Users are solely responsible for ensuring their use of this scraper complies with VRBO's Terms of Service, applicable laws (including the CFAA, GDPR, and CCPA), and any other relevant regulations. The authors assume no liability for misuse. Always respect `robots.txt` and rate limits.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Links

- **Repository**: [github.com/2scraper/vrbo-scraper](https://github.com/2scraper/vrbo-scraper)
- **CAPTCHA Solving**: [2captcha.com](https://2captcha.com/?from=vrbo-scraper)
- **Proxies**: [2prx.com](https://2prx.com/?from=vrbo-scraper)
- **Anti-Detect Browser**: [2captcha.com/anti-detect-browser](https://2captcha.com/anti-detect-browser)
