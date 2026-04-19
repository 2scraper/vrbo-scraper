/**
 * VRBO Scraper — Puppeteer Edition
 * ==================================
 * Node.js implementation using puppeteer-extra with stealth plugin.
 *
 * Strategy (three-layer extraction):
 *   1. Intercept XHR / GraphQL responses
 *   2. Parse embedded JSON (__NEXT_DATA__, ld+json, etc.)
 *   3. DOM scraping with adaptive selectors
 *
 * Repository : https://github.com/2scraper/vrbo-scraper
 * CAPTCHA API: https://2captcha.com
 * Proxies    : https://2prx.com
 *
 * Usage:
 *   npm install puppeteer puppeteer-extra puppeteer-extra-plugin-stealth 2captcha
 *   node vrbo_scraper_puppeteer.js --destination "Orlando, FL"
 */

const puppeteer = require("puppeteer-extra");
const StealthPlugin = require("puppeteer-extra-plugin-stealth");
const fs = require("fs");
puppeteer.use(StealthPlugin());

let TwoCaptcha;
try { TwoCaptcha = require("2captcha").Solver; } catch { TwoCaptcha = null; }

const BASE_URL = "https://www.vrbo.com";
const USER_AGENTS = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
];
const VIEWPORTS = [
  { width: 1920, height: 1080 }, { width: 1366, height: 768 },
  { width: 1536, height: 864 },  { width: 1440, height: 900 },
];

const pick = a => a[Math.floor(Math.random() * a.length)];
const rand = (lo, hi) => Math.random() * (hi - lo) + lo;
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function hdelay(lo = 800, hi = 2500) { await sleep(rand(lo, hi)); }
async function hscroll(page, n = 3) {
  for (let i = 0; i < n; i++) {
    await page.evaluate(d => window.scrollBy(0, d), Math.floor(rand(250, 700)));
    await sleep(rand(300, 800));
  }
}
function log(m, ...a) { console.log(`${new Date().toISOString().slice(11,19)}  INFO     ${m}`, ...a); }
function warn(m, ...a) { console.warn(`${new Date().toISOString().slice(11,19)}  WARN     ${m}`, ...a); }

const STEALTH_EXTRA = `
  Object.defineProperty(navigator,'platform',{get:()=>'${pick(["Win32","MacIntel","Linux x86_64"])}'});
  Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>${pick([4,8,12])}});
  Object.defineProperty(navigator,'deviceMemory',{get:()=>${pick([4,8,16])}});
  const _gp=WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter=function(p){
    if(p===37445)return'Intel Inc.';
    if(p===37446)return'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)';
    return _gp.call(this,p);
  };
`;

// ── CAPTCHA ────────────────────────────────────────────────────────────────

class CaptchaSolver {
  constructor(key) { this.solver = TwoCaptcha && key ? new TwoCaptcha(key) : null; }
  get enabled() { return !!this.solver; }
  async detectAndSolve(page) {
    if (!this.enabled) return false;
    const html = await page.content();
    if (/recaptcha\/api2|recaptcha\/enterprise/.test(html)) return this._rc2(page, html);
    if (html.includes("hcaptcha.com")) return this._hc(page, html);
    if (html.includes("challenges.cloudflare.com") || html.includes("cf-turnstile")) return this._ts(page, html);
    return false;
  }
  _sk(html, pat) { const m = html.match(pat); return m ? m[1] : null; }
  async _rc2(page, html) {
    const sk = this._sk(html, /data-sitekey=["']([^"']+)/) || this._sk(html, /recaptcha\/api2\/anchor\?.*?k=([A-Za-z0-9_-]+)/);
    if (!sk) return false;
    const r = await this.solver.recaptcha(sk, page.url());
    const t = typeof r === "object" ? r.data : r;
    await page.evaluate(t => { document.getElementById("g-recaptcha-response").value = t; }, t);
    log("reCAPTCHA v2 solved ✓"); return true;
  }
  async _hc(page, html) {
    const sk = this._sk(html, /data-sitekey=["']([^"']+)/);
    if (!sk) return false;
    const r = await this.solver.hcaptcha(sk, page.url());
    const t = typeof r === "object" ? r.data : r;
    await page.evaluate(t => {
      const h = document.querySelector('[name="h-captcha-response"]');
      const g = document.querySelector('[name="g-recaptcha-response"]');
      if (h) h.value = t; if (g) g.value = t;
    }, t);
    log("hCaptcha solved ✓"); return true;
  }
  async _ts(page, html) {
    const sk = this._sk(html, /data-sitekey=["']([^"']+)/);
    if (!sk) return false;
    const r = await this.solver.turnstile(sk, page.url());
    const t = typeof r === "object" ? r.data : r;
    await page.evaluate(t => {
      const c = document.querySelector('[name="cf-turnstile-response"]'); if (c) c.value = t;
    }, t);
    log("Turnstile solved ✓"); return true;
  }
}

// ── API Interceptor ────────────────────────────────────────────────────────

const API_PATS = ["graphql","/api/","/mapi/","/serp/","bex-api","PropertySearch","propertySearch","SearchResult","LodgingPwa","searchResults"];

class APIInterceptor {
  constructor() { this.captured = []; }
  matches(url) { const ul = url.toLowerCase(); return API_PATS.some(p => ul.includes(p.toLowerCase())); }
  async handle(response) {
    if (!this.matches(response.url())) return;
    try {
      const ct = response.headers()["content-type"] || "";
      if (!ct.includes("json") && !response.url().toLowerCase().includes("graphql")) return;
      const body = await response.text();
      if (body.length < 200) return;
      this.captured.push(JSON.parse(body));
      log(`  ✓ API intercept: ${response.url().slice(0,100)} (${body.length}b)`);
    } catch {}
  }
  extract() {
    const out = []; const seen = new Set();
    for (const blob of this.captured) {
      for (const item of walkJson(blob)) {
        const k = item.property_id || item.title || "";
        if (k && !seen.has(k)) { seen.add(k); out.push(item); }
      }
    }
    this.captured = [];
    return out;
  }
}

// ── JSON walker ────────────────────────────────────────────────────────────

function walkJson(obj, depth = 0) {
  if (depth > 15) return [];
  const results = [];
  if (obj && typeof obj === "object" && !Array.isArray(obj)) {
    if (isListing(obj)) { const p = parseListing(obj); if (p) results.push(p); }
    else for (const v of Object.values(obj)) results.push(...walkJson(v, depth + 1));
  } else if (Array.isArray(obj)) {
    for (const item of obj) results.push(...walkJson(item, depth + 1));
  }
  return results;
}

function isListing(d) {
  const kl = new Set(Object.keys(d).map(k => k.toLowerCase()));
  let hasName = ["name","title","headline","propertyname","headlinetext","listing_name"].some(n => kl.has(n));
  const hasId = ["propertyid","listingid","property_id","id","unitid","price","rateprice","nightlyrate","averageprice"].some(n => kl.has(n));
  if (!hasName) for (const v of Object.values(d))
    if (v && typeof v === "object" && !Array.isArray(v)) {
      const nk = new Set(Object.keys(v).map(k => k.toLowerCase()));
      if (["name","title","headline"].some(n => nk.has(n))) { hasName = true; break; }
    }
  return hasName && hasId;
}

function fv(obj, ...names) {
  if (!obj || typeof obj !== "object") return null;
  for (const [k, v] of Object.entries(obj))
    if (names.some(n => k.toLowerCase() === n.toLowerCase())) return v;
  for (const v of Object.values(obj))
    if (v && typeof v === "object" && !Array.isArray(v))
      for (const [k2, v2] of Object.entries(v))
        if (names.some(n => k2.toLowerCase() === n.toLowerCase())) return v2;
  return null;
}

function parseListing(d) {
  const title = fv(d, "name","title","headline","propertyName","headlineText");
  if (!title) return null;
  const pid = fv(d, "propertyId","listingId","property_id","id","unitId");
  let url = fv(d, "url","detailUrl","deepLink","propertyUrl","pdpUrl","href");
  if (url && !url.startsWith("http")) url = BASE_URL + (url.startsWith("/") ? "" : "/") + url;

  let ppn = null, pt = null;
  const pr = fv(d, "price","ratePrice","averagePrice","nightlyRate","leadPrice","displayPrice","totalPrice");
  if (typeof pr === "number") ppn = Math.round(pr);
  else if (typeof pr === "string") { pt = pr; const m = pr.match(/\$?([\d,]+)/); if (m) ppn = parseInt(m[1].replace(/,/g, "")); }
  else if (pr && typeof pr === "object") {
    const a = fv(pr, "amount","value","formatted","displayPrice");
    if (typeof a === "number") ppn = Math.round(a);
    else if (typeof a === "string") { pt = a; const m = a.match(/\$?([\d,]+)/); if (m) ppn = parseInt(m[1].replace(/,/g, "")); }
  }

  let rating = null;
  const rr = fv(d, "rating","averageRating","reviewScore","overallRating","guestRating");
  if (typeof rr === "number") rating = rr;
  else if (rr && typeof rr === "object") { const rv = fv(rr, "value","overall","score"); if (typeof rv === "number") rating = rv; }

  const rc = fv(d, "reviewCount","reviews_count","totalReviews","numberOfReviews");
  const bed = fv(d, "bedrooms","bedroomCount");
  const bath = fv(d, "bathrooms","bathroomCount");
  const slp = fv(d, "sleeps","maxOccupancy","guestCount");
  let pt2 = fv(d, "propertyType","type","lodgingType");
  if (pt2 && typeof pt2 === "object") pt2 = fv(pt2, "name","label");

  let iurl = null;
  const img = fv(d, "image","thumbnail","heroImage","primaryImage","photo");
  if (typeof img === "string") iurl = img;
  else if (img && typeof img === "object" && !Array.isArray(img)) iurl = fv(img, "url","src","uri");
  else if (Array.isArray(img) && img.length) {
    const f = img[0]; iurl = typeof f === "string" ? f : (f ? fv(f, "url","src") : null);
  }

  return {
    title: String(title), property_id: pid ? String(pid) : null, url,
    price_per_night: ppn, price_text: pt, rating,
    reviews_count: typeof rc === "number" ? Math.round(rc) : null,
    bedrooms: typeof bed === "number" ? Math.round(bed) : null,
    bathrooms: typeof bath === "number" ? bath : null,
    sleeps: typeof slp === "number" ? Math.round(slp) : null,
    property_type: pt2 ? String(pt2) : null, image_url: iurl,
  };
}

// ── Embedded JSON extraction ───────────────────────────────────────────────

const EMBEDDED_FN = () => {
  const r = [];
  const nd = document.getElementById("__NEXT_DATA__");
  if (nd) try { r.push(JSON.parse(nd.textContent)); } catch {}
  for (const k of ["__CONFIG__","__INITIAL_STATE__","__PRELOADED_STATE__","__DATA__"])
    if (window[k]) r.push(window[k]);
  document.querySelectorAll('script[type="application/json"],script[type="application/ld+json"]').forEach(el => {
    try { if (el.textContent.length > 500) r.push(JSON.parse(el.textContent)); } catch {}
  });
  document.querySelectorAll("script:not([src])").forEach(el => {
    const t = el.textContent || "";
    if (t.length > 2000 && (t.includes("propertyId") || t.includes('"bedrooms"') || t.includes("searchResults"))) {
      const m = t.match(/(?:window\.\w+\s*=\s*)(\{[\s\S]+\})\s*;?\s*$/);
      if (m) try { r.push(JSON.parse(m[1])); } catch {}
    }
  });
  return r;
};

// ── DOM extraction ─────────────────────────────────────────────────────────

const DOM_FN = () => {
  const results = [];
  const propPat = [/\/\d{5,}/, /\/vacation-rentals\//, /\/unit\//, /\/lodging\//, /\/property\//];
  const isPL = h => propPat.some(p => p.test(h));
  const plinks = [];
  for (const a of document.querySelectorAll("a[href]")) {
    const h = a.getAttribute("href") || "";
    if (isPL(h) && a.offsetHeight > 30) plinks.push(a);
  }
  const done = new Set();
  for (const link of plinks) {
    let card = link;
    for (let i = 0; i < 8; i++) {
      if (!card.parentElement) break; card = card.parentElement;
      if (card.querySelector("img") && (card.innerText || "").length > 30) break;
    }
    if (done.has(card)) continue; done.add(card);
    try {
      const txt = card.innerText || "";
      const href = link.getAttribute("href") || "";
      const url = href.startsWith("http") ? href : "https://www.vrbo.com" + href;
      const tEl = card.querySelector('h1,h2,h3,h4,[class*="title"],[class*="name"]');
      const title = tEl ? tEl.innerText.trim() : null;
      if (!title || title.length < 3) continue;
      const pm = txt.match(/\$([\d,]+)/);
      const rm = txt.match(/(\d\.\d)\s*(?:\/|out|star|\()/i);
      const rvm = txt.match(/(\d[\d,]*)\s*review/i);
      const idm = url.match(/\/(\d{5,})/);
      const bm = txt.match(/(\d+)\s*(?:BR|bed(?:room)?s?)/i);
      const btm = txt.match(/(\d+\.?\d*)\s*(?:BA|bath)/i);
      const sm = txt.match(/(?:sleep|accommodat)\w*\s*(\d+)/i);
      const img = card.querySelector('img[src*="http"]');
      const tm = txt.match(/\b(House|Condo|Cabin|Apartment|Villa|Cottage|Chalet|Studio|Townhouse|Resort|Lodge)\b/i);
      results.push({
        title, property_id: idm ? idm[1] : null, url,
        price_per_night: pm ? parseInt(pm[1].replace(/,/g, "")) : null,
        rating: rm ? parseFloat(rm[1]) : null,
        reviews_count: rvm ? parseInt(rvm[1].replace(/,/g, "")) : null,
        bedrooms: bm ? parseInt(bm[1]) : null, bathrooms: btm ? parseFloat(btm[1]) : null,
        sleeps: sm ? parseInt(sm[1]) : null, property_type: tm ? tm[1] : null,
        image_url: img ? img.getAttribute("src") : null,
      });
    } catch {}
  }
  return results;
};

// ── Main ───────────────────────────────────────────────────────────────────

function dedupe(arr) {
  const seen = new Set(); return arr.filter(i => {
    const k = i.property_id || i.url || i.title || "";
    if (!k || seen.has(k)) return false; seen.add(k); return true;
  });
}

async function scrapeVrbo(opts) {
  const {
    destination, checkin, checkout, maxPages = 5, maxProperties = 0,
    detailPages = false, outputFormat = "json", outputFile,
    proxy, captchaKey, headless = true, debug = false,
  } = opts;

  const solver = new CaptchaSolver(captchaKey);
  const allListings = [];

  const args = ["--disable-blink-features=AutomationControlled","--disable-infobars","--no-first-run"];
  if (proxy) { args.push(`--proxy-server=${proxy}`); log("Proxy:", proxy.includes("@") ? proxy.split("@").pop() : proxy); }

  const browser = await puppeteer.launch({ headless: headless ? "new" : false, args });
  const page = await browser.newPage();
  await page.setViewport(pick(VIEWPORTS));
  await page.setUserAgent(pick(USER_AGENTS));
  await page.evaluateOnNewDocument(STEALTH_EXTRA);
  await page.setExtraHTTPHeaders({ "Accept-Language": "en-US,en;q=0.9" });

  // API interceptor
  const interceptor = new APIInterceptor();
  page.on("response", r => interceptor.handle(r));

  try {
    const params = new URLSearchParams({ destination });
    if (checkin) params.set("startDate", checkin);
    if (checkout) params.set("endDate", checkout);
    const searchUrl = `${BASE_URL}/search?${params}`;

    log("Opening:", searchUrl);
    await page.goto(searchUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
    log("URL:", page.url());

    log("Waiting for JS …");
    await hdelay(5000, 8000);
    await hscroll(page, 4);
    await hdelay(2000, 3000);

    if (solver.enabled) { await solver.detectAndSolve(page); await hdelay(1000, 2000); }

    if (debug) {
      await page.screenshot({ path: "vrbo_debug.png" });
      log("Title:", await page.title());
    }

    let pagesDone = 0;
    while (pagesDone < maxPages) {
      pagesDone++;
      log(`--- Page ${pagesDone} / ${maxPages} ---`);
      await hscroll(page, 4); await hdelay(2000, 3000);

      const s1 = interceptor.extract();
      const blobs = await page.evaluate(EMBEDDED_FN);
      let s2 = [];
      for (const blob of blobs) s2.push(...walkJson(blob));
      const s3 = await page.evaluate(DOM_FN);
      const pageList = dedupe([...s1, ...s2, ...s3]);

      if (!pageList.length) {
        warn(`No listings (api=${s1.length} emb=${s2.length} dom=${s3.length}). Try --debug or --headed.`);
        if (debug) await page.screenshot({ path: `vrbo_debug_p${pagesDone}.png` });
        break;
      }

      log(`Found ${pageList.length} (api=${s1.length} emb=${s2.length} dom=${s3.length})`);

      if (detailPages) {
        for (let i = 0; i < pageList.length; i++) {
          const lst = pageList[i];
          if (!lst.url) continue;
          log(`  → detail ${i+1}/${pageList.length}: ${(lst.title || "").slice(0, 50)}`);
          const dp = await browser.newPage();
          await dp.setViewport(pick(VIEWPORTS));
          await dp.evaluateOnNewDocument(STEALTH_EXTRA);
          try {
            await dp.goto(lst.url, { waitUntil: "domcontentloaded", timeout: 45000 });
            await hdelay(1500, 3000);
            if (solver.enabled) await solver.detectAndSolve(dp);
            const det = await dp.evaluate(() => {
              const txt = s => { const e = document.querySelector(s); return e ? e.innerText.trim() : null; };
              let desc = null;
              document.querySelectorAll('p,[class*="description"]').forEach(e => {
                const t = e.innerText.trim(); if (t.length > (desc || "").length && t.length > 50) desc = t;
              });
              const am = [];
              document.querySelectorAll('[class*="amenity"] li').forEach(e => {
                const a = e.innerText.trim(); if (a && a.length < 80) am.push(a);
              });
              return { title: txt("h1"), description: desc ? desc.substring(0, 2000) : null, amenities: [...new Set(am)] };
            });
            Object.entries(det).forEach(([k, v]) => { if (v) lst[k] = v; });
          } catch (e) { warn("Detail error:", e.message); }
          finally { await dp.close(); }
          await hdelay(1000, 2500);
        }
      }

      allListings.push(...pageList);
      log(`Total: ${allListings.length}`);
      if (maxProperties > 0 && allListings.length >= maxProperties) { allListings.length = maxProperties; break; }

      if (pagesDone < maxPages) {
        const nextBtn = await page.$('a[aria-label*="Next"],button[aria-label*="Next"],a[rel="next"]');
        if (!nextBtn) { log("No more pages."); break; }
        await nextBtn.click();
        await hdelay(3000, 5000);
        if (solver.enabled) await solver.detectAndSolve(page);
      }
    }
  } finally { await browser.close(); }

  const ts = new Date().toISOString();
  for (const i of allListings) { i.scraped_at = ts; i.source = "vrbo.com"; }

  const safeDest = destination.replace(/[^a-zA-Z0-9]+/g, "_").toLowerCase();
  const base = outputFile || `vrbo_${safeDest}_${ts.replace(/[^0-9]/g, "").slice(0, 14)}`;

  if (outputFormat === "csv") {
    const fp = base.endsWith(".csv") ? base : base + ".csv";
    if (allListings.length) {
      const keys = [...new Set(allListings.flatMap(i => Object.keys(i)))];
      const rows = allListings.map(i => keys.map(k => {
        let v = i[k]; if (v == null) return "";
        if (Array.isArray(v)) v = v.join("; ");
        return `"${String(v).replace(/"/g, '""')}"`;
      }).join(","));
      fs.writeFileSync(fp, [keys.join(","), ...rows].join("\n"), "utf-8");
    }
    log(`Saved ${allListings.length} → ${fp}`);
  } else {
    const fp = base.endsWith(".json") ? base : base + ".json";
    fs.writeFileSync(fp, JSON.stringify(allListings, null, 2), "utf-8");
    log(`Saved ${allListings.length} → ${fp}`);
  }
  return allListings;
}

// ── CLI ────────────────────────────────────────────────────────────────────

function parseArgs() {
  const a = process.argv.slice(2), o = {};
  for (let i = 0; i < a.length; i++) {
    if (a[i] === "--destination" || a[i] === "-d") o.destination = a[++i];
    else if (a[i] === "--checkin") o.checkin = a[++i];
    else if (a[i] === "--checkout") o.checkout = a[++i];
    else if (a[i] === "--max-pages") o.maxPages = parseInt(a[++i]);
    else if (a[i] === "--max-properties") o.maxProperties = parseInt(a[++i]);
    else if (a[i] === "--details") o.detailPages = true;
    else if (a[i] === "--format") o.outputFormat = a[++i];
    else if (a[i] === "--output" || a[i] === "-o") o.outputFile = a[++i];
    else if (a[i] === "--proxy") o.proxy = a[++i];
    else if (a[i] === "--captcha-key") o.captchaKey = a[++i];
    else if (a[i] === "--headed") o.headless = false;
    else if (a[i] === "--debug") o.debug = true;
  }
  return o;
}

(async () => {
  const opts = parseArgs();
  if (!opts.destination) { console.error('Usage: node vrbo_scraper_puppeteer.js --destination "Orlando, FL"'); process.exit(1); }
  const results = await scrapeVrbo(opts);
  console.log(`\nDone — ${results.length} properties scraped.`);
})();
