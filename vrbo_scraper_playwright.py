"""
VRBO Scraper — Playwright Edition (Primary)
============================================
Open-source vacation rental scraper for vrbo.com.

Anti-detection approach:
  - Uses real Chrome browser (not bundled Chromium) to pass TLS fingerprinting
  - Warm-up phase: visits homepage first, accepts cookies, then searches
  - Three-layer extraction: API interception → embedded JSON → adaptive DOM

Features:
  - 2captcha.com integration (reCAPTCHA v2/v3, hCaptcha, Turnstile)
  - Proxy support via 2prx.com (recommended for Akamai bypass)
  - Fingerprint randomization & human-like behavior
  - JSON / CSV output

Repository : https://github.com/2scraper/vrbo-scraper
CAPTCHA API: https://2captcha.com
Proxies    : https://2prx.com

Setup:
  pip install playwright twocaptcha-python
  playwright install chrome        # <-- installs real Chrome, NOT Chromium!

Usage:
  python vrbo_scraper_playwright.py --destination "Orlando, FL"
  python vrbo_scraper_playwright.py --destination "Maui, HI" --proxy "http://user:pass@gate.2prx.com:8080"
"""

import argparse, asyncio, csv, json, logging, os, random, re, sys, time
from datetime import datetime
from urllib.parse import urlencode, urlparse, parse_qs, urlencode as ue

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("playwright is required.  Install: pip install playwright && playwright install chrome")

try:
    from twocaptcha import TwoCaptcha
except ImportError:
    TwoCaptcha = None

BASE_URL = "https://www.vrbo.com"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]
VIEWPORTS = [
    {"width": 1920, "height": 1080}, {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},  {"width": 1440, "height": 900},
]
WEBGL_VENDORS = ["Intel Inc.", "Google Inc. (NVIDIA)", "Google Inc. (Intel)"]
PLATFORMS = ["Win32", "MacIntel", "Linux x86_64"]
LANGUAGES_LIST = [["en-US", "en"], ["en-US"], ["en-GB", "en"]]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("vrbo-pw")

# ── Stealth ─────────────────────────────────────────────────────────────────

STEALTH_JS = """
() => {
    // Core webdriver hide
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    delete navigator.__proto__.webdriver;

    // Plugins
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1,2,3,4,5].map(() => ({name:'Chrome PDF Plugin',description:'PDF',filename:'internal-pdf-viewer',length:1})),
    });

    // Fingerprint values
    Object.defineProperty(navigator, 'languages', {get: () => __LANGUAGES__});
    Object.defineProperty(navigator, 'platform',  {get: () => '__PLATFORM__'});
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => __CORES__});
    Object.defineProperty(navigator, 'deviceMemory', {get: () => __MEMORY__});

    // WebGL
    const gp = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        if (p===37445) return '__WEBGL_VENDOR__';
        if (p===37446) return 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)';
        return gp.call(this, p);
    };

    // Chrome object
    window.chrome = {
        runtime: {
            connect: () => {},
            sendMessage: () => {},
            onMessage: {addListener: () => {}, removeListener: () => {}},
            onConnect: {addListener: () => {}, removeListener: () => {}},
        },
        loadTimes: () => ({
            commitLoadTime: Date.now() / 1000,
            connectionInfo: 'h2',
            finishDocumentLoadTime: Date.now() / 1000 + 0.3,
            finishLoadTime: Date.now() / 1000 + 0.5,
            firstPaintAfterLoadTime: 0,
            firstPaintTime: Date.now() / 1000 + 0.1,
            navigationType: 'Other',
            npnNegotiatedProtocol: 'h2',
            requestTime: Date.now() / 1000 - 0.5,
            startLoadTime: Date.now() / 1000 - 0.5,
            wasAlternateProtocolAvailable: false,
            wasFetchedViaSpdy: true,
            wasNpnNegotiated: true,
        }),
        csi: () => ({
            onloadT: Date.now(),
            pageT: Date.now() - performance.timing.navigationStart,
            startE: performance.timing.navigationStart,
            tran: 15,
        }),
    };

    // Permissions
    const oq = window.navigator.permissions.query;
    window.navigator.permissions.query = (p) =>
        p.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : oq(p);

    // Connection
    if (!navigator.connection) {
        Object.defineProperty(navigator, 'connection', {
            get: () => ({effectiveType: '4g', rtt: 50, downlink: 10, saveData: false})
        });
    }

    // Prevent automation detection via stack traces
    const originalError = Error;
    Error = function(...args) {
        const err = new originalError(...args);
        const stack = err.stack || '';
        err.stack = stack.replace(/\\n\\s+at\\s+.*playwright.*\\n/g, '\\n');
        return err;
    };
    Error.prototype = originalError.prototype;
}
"""

def build_stealth_script():
    s = STEALTH_JS
    s = s.replace("__LANGUAGES__", json.dumps(random.choice(LANGUAGES_LIST)))
    s = s.replace("__PLATFORM__", random.choice(PLATFORMS))
    s = s.replace("__CORES__", str(random.choice([4,8,12,16])))
    s = s.replace("__MEMORY__", str(random.choice([4,8,16])))
    s = s.replace("__WEBGL_VENDOR__", random.choice(WEBGL_VENDORS))
    return s


# ── Human behaviour ─────────────────────────────────────────────────────────

async def human_delay(lo=0.8, hi=2.5):
    await asyncio.sleep(random.uniform(lo, hi))

async def human_scroll(page, steps=3):
    for _ in range(steps):
        await page.mouse.wheel(0, random.randint(200, 600))
        await asyncio.sleep(random.uniform(0.4, 1.0))

async def random_mouse_move(page):
    vp = page.viewport_size or {"width":1280,"height":720}
    await page.mouse.move(random.randint(100,vp["width"]-100),
                          random.randint(100,vp["height"]-100),
                          steps=random.randint(8,20))

async def human_type(page, selector, text):
    """Type text with human-like delays between keystrokes."""
    el = await page.wait_for_selector(selector, timeout=10000)
    await el.click()
    for ch in text:
        await page.keyboard.type(ch, delay=random.randint(50, 150))
        if random.random() < 0.05:
            await asyncio.sleep(random.uniform(0.2, 0.5))


# ── CAPTCHA (2captcha.com) ──────────────────────────────────────────────────

class CaptchaSolver:
    def __init__(self, api_key):
        self.solver = TwoCaptcha(api_key) if (TwoCaptcha and api_key) else None

    @property
    def enabled(self): return self.solver is not None

    async def detect_and_solve(self, page):
        if not self.enabled: return False
        ct = await self._detect(page)
        if not ct: return False
        log.info("CAPTCHA detected: %s — solving via 2captcha.com …", ct)
        try:
            if ct=="recaptcha_v2": return await self._rc2(page)
            if ct=="hcaptcha":     return await self._hc(page)
            if ct=="turnstile":    return await self._ts(page)
        except Exception as e:
            log.error("CAPTCHA solve failed: %s", e)
        return False

    async def _detect(self, page):
        for ct, sel in [
            ("recaptcha_v2","iframe[src*='recaptcha/api2']"),
            ("recaptcha_v2","iframe[src*='recaptcha/enterprise']"),
            ("hcaptcha","iframe[src*='hcaptcha.com']"),
            ("turnstile","iframe[src*='challenges.cloudflare.com']"),
            ("turnstile","[class*='cf-turnstile']"),
        ]:
            if await page.query_selector(sel): return ct
        return None

    async def _sitekey(self, page, pat):
        m = re.search(pat, await page.content())
        return m.group(1) if m else None

    async def _rc2(self, page):
        sk = await self._sitekey(page, r'data-sitekey=["\']([^"\']+)') \
             or await self._sitekey(page, r'recaptcha/api2/anchor\?.*?k=([A-Za-z0-9_-]+)')
        if not sk: return False
        r = await asyncio.to_thread(self.solver.recaptcha, sitekey=sk, url=page.url)
        t = r.get("code") if isinstance(r,dict) else r
        await page.evaluate(f"document.getElementById('g-recaptcha-response').value='{t}';")
        log.info("reCAPTCHA v2 solved ✓"); return True

    async def _hc(self, page):
        sk = await self._sitekey(page, r'data-sitekey=["\']([^"\']+)')
        if not sk: return False
        r = await asyncio.to_thread(self.solver.hcaptcha, sitekey=sk, url=page.url)
        t = r.get("code") if isinstance(r,dict) else r
        await page.evaluate(f"""
            document.querySelector('[name="h-captcha-response"]').value='{t}';
            document.querySelector('[name="g-recaptcha-response"]').value='{t}';
        """)
        log.info("hCaptcha solved ✓"); return True

    async def _ts(self, page):
        sk = await self._sitekey(page, r'data-sitekey=["\']([^"\']+)')
        if not sk: return False
        r = await asyncio.to_thread(self.solver.turnstile, sitekey=sk, url=page.url)
        t = r.get("code") if isinstance(r,dict) else r
        await page.evaluate(f"""
            const c=document.querySelector('[name="cf-turnstile-response"]');if(c)c.value='{t}';
        """)
        log.info("Turnstile solved ✓"); return True


# ── API Interceptor ─────────────────────────────────────────────────────────

class APIInterceptor:
    PATTERNS = [
        "graphql", "/api/", "/mapi/", "/serp/", "bex-api",
        "PropertySearch", "propertySearch", "SearchResult",
        "LodgingPwa", "/shopping/", "searchResults",
    ]

    def __init__(self):
        self.captured: list[dict] = []

    def _matches(self, url):
        ul = url.lower()
        return any(p.lower() in ul for p in self.PATTERNS)

    async def handle(self, response):
        url = response.url
        if not self._matches(url): return
        try:
            ct = response.headers.get("content-type","")
            if "json" not in ct and "graphql" not in url.lower(): return
            body = await response.text()
            if len(body) < 200: return
            data = json.loads(body)
            self.captured.append(data)
            log.info("  ✓ API intercept: %s (%d bytes)", url[:100], len(body))
        except: pass

    def extract(self) -> list[dict]:
        listings = []; seen = set()
        for blob in self.captured:
            for item in self._walk(blob):
                key = item.get("property_id") or item.get("title","")
                if key and key not in seen:
                    seen.add(key); listings.append(item)
        self.captured.clear()
        return listings

    def _walk(self, obj, depth=0):
        if depth > 15: return []
        results = []
        if isinstance(obj, dict):
            if self._looks_like_listing(obj):
                p = self._parse(obj)
                if p: results.append(p)
            else:
                for v in obj.values():
                    results.extend(self._walk(v, depth+1))
        elif isinstance(obj, list):
            for item in obj: results.extend(self._walk(item, depth+1))
        return results

    def _looks_like_listing(self, d):
        kl = {k.lower() for k in d}
        has_name = bool(kl & {"name","title","headline","propertyname","headlinetext","listing_name"})
        has_id = bool(kl & {"propertyid","listingid","property_id","id","unitid",
                            "price","rateprice","nightlyrate","pricesummary","averageprice"})
        if not has_name:
            for v in d.values():
                if isinstance(v, dict) and ({k.lower() for k in v} & {"name","title","headline"}):
                    has_name = True; break
        return has_name and has_id

    def _parse(self, d):
        def fv(obj, *names):
            if not isinstance(obj, dict): return None
            for k,v in obj.items():
                if k.lower() in [n.lower() for n in names]: return v
            for v in obj.values():
                if isinstance(v, dict):
                    for k2,v2 in v.items():
                        if k2.lower() in [n.lower() for n in names]: return v2
            return None

        title = fv(d,"name","title","headline","propertyName","headlineText","listingName")
        if not title: return None
        prop_id = fv(d,"propertyId","listingId","property_id","id","unitId")
        url = fv(d,"url","detailUrl","deepLink","propertyUrl","pdpUrl","href","landingUrl")
        if url and not url.startswith("http"):
            url = BASE_URL + ("" if url.startswith("/") else "/") + url

        pr = fv(d,"price","ratePrice","averagePrice","nightlyRate","leadPrice","displayPrice","totalPrice")
        ppn, pt = None, None
        if isinstance(pr,(int,float)): ppn=int(pr)
        elif isinstance(pr,str):
            pt=pr; m=re.search(r'\$?([\d,]+)',pr)
            if m: ppn=int(m.group(1).replace(",",""))
        elif isinstance(pr,dict):
            a=fv(pr,"amount","value","formatted","displayPrice","lead","total")
            if isinstance(a,(int,float)): ppn=int(a)
            elif isinstance(a,str):
                pt=a; m=re.search(r'\$?([\d,]+)',a)
                if m: ppn=int(m.group(1).replace(",",""))

        rr=fv(d,"rating","averageRating","reviewScore","overallRating","guestRating")
        rating=float(rr) if isinstance(rr,(int,float)) else None
        if isinstance(rr,dict):
            rv=fv(rr,"value","overall","score","average")
            if isinstance(rv,(int,float)): rating=float(rv)

        rc=fv(d,"reviewCount","reviews_count","totalReviews","numberOfReviews")
        reviews=int(rc) if isinstance(rc,(int,float)) else None
        bed=fv(d,"bedrooms","bedroomCount","numberOfBedrooms")
        bed=int(bed) if isinstance(bed,(int,float)) else None
        bath=fv(d,"bathrooms","bathroomCount","numberOfBathrooms")
        bath=float(bath) if isinstance(bath,(int,float)) else None
        slp=fv(d,"sleeps","maxOccupancy","guestCount","maxGuests")
        slp=int(slp) if isinstance(slp,(int,float)) else None
        pt2=fv(d,"propertyType","type","lodgingType","category")
        if isinstance(pt2,dict): pt2=fv(pt2,"name","label","text")

        img=fv(d,"image","thumbnail","heroImage","primaryImage","photo")
        iurl=None
        if isinstance(img,str): iurl=img
        elif isinstance(img,dict): iurl=fv(img,"url","src","uri")
        elif isinstance(img,list) and img:
            f=img[0]; iurl=f if isinstance(f,str) else fv(f,"url","src","uri") if isinstance(f,dict) else None

        return {
            "title":str(title), "property_id":str(prop_id) if prop_id else None,
            "url":url, "price_per_night":ppn, "price_text":pt,
            "rating":rating, "reviews_count":reviews,
            "bedrooms":bed, "bathrooms":bath, "sleeps":slp,
            "property_type":str(pt2) if pt2 else None, "image_url":iurl,
        }


# ── Embedded JSON ───────────────────────────────────────────────────────────

async def extract_embedded_json(page) -> list[dict]:
    blobs = await page.evaluate("""
    () => {
        const r = [];
        const nd = document.getElementById('__NEXT_DATA__');
        if (nd) try { r.push(JSON.parse(nd.textContent)); } catch {}
        for (const k of ['__CONFIG__','__INITIAL_STATE__','__PRELOADED_STATE__','__DATA__'])
            if (window[k]) r.push(window[k]);
        document.querySelectorAll('script[type="application/json"],script[type="application/ld+json"]').forEach(el => {
            try { if (el.textContent.length > 500) r.push(JSON.parse(el.textContent)); } catch {}
        });
        document.querySelectorAll('script:not([src])').forEach(el => {
            const t = el.textContent || '';
            if (t.length>2000 && (t.includes('propertyId')||t.includes('"bedrooms"')||t.includes('searchResults'))) {
                const m = t.match(/(?:window\\.\\w+\\s*=\\s*)(\\{[\\s\\S]+\\})\\s*;?\\s*$/);
                if (m) try { r.push(JSON.parse(m[1])); } catch {}
            }
        });
        return r;
    }
    """)
    inter = APIInterceptor()
    for blob in blobs: inter.captured.append(blob)
    listings = inter.extract()
    if listings:
        log.info("Embedded JSON: %d listings from %d blobs", len(listings), len(blobs))
    return listings


# ── DOM extraction ──────────────────────────────────────────────────────────

async def extract_listings_dom(page) -> list[dict]:
    return await page.evaluate("""
    () => {
        const results = [];
        const propPat = [/\\/\\d{5,}/, /\\/vacation-rentals\\//, /\\/unit\\//, /\\/lodging\\//, /\\/property\\//];
        const isPL = h => propPat.some(p => p.test(h));

        const plinks = [];
        for (const a of document.querySelectorAll('a[href]')) {
            const h = a.getAttribute('href')||'';
            if (isPL(h) && a.offsetHeight > 30) plinks.push(a);
        }

        const done = new Set();
        for (const link of plinks) {
            let card = link;
            for (let i=0; i<8; i++) {
                if (!card.parentElement) break; card = card.parentElement;
                if (card.querySelector('img') && (card.innerText||'').length > 30) break;
            }
            if (done.has(card)) continue; done.add(card);
            try {
                const txt = card.innerText||'';
                const href = link.getAttribute('href')||'';
                const url = href.startsWith('http') ? href : 'https://www.vrbo.com'+href;
                const tEl = card.querySelector('h1,h2,h3,h4,[class*="title" i],[class*="name" i]')
                           || link.querySelector('h1,h2,h3,h4');
                const title = tEl ? tEl.innerText.trim() : null;
                if (!title || title.length<3) continue;
                const pm = txt.match(/\\$(\\d[\\d,]*)/);
                const rm = txt.match(/(\\d\\.\\d)\\s*(?:\\/|out|star|\\()/i);
                const rvm = txt.match(/(\\d[\\d,]*)\\s*review/i);
                const idm = url.match(/\\/(\\d{5,})/);
                const bm = txt.match(/(\\d+)\\s*(?:BR|bed(?:room)?s?)/i);
                const btm = txt.match(/(\\d+\\.?\\d*)\\s*(?:BA|bath)/i);
                const sm = txt.match(/(?:sleep|accommodat)\\w*\\s*(\\d+)/i) || txt.match(/(\\d+)\\s*guest/i);
                const img = card.querySelector('img[src*="http"]');
                const tm = txt.match(/\\b(House|Condo|Cabin|Apartment|Villa|Cottage|Chalet|Studio|Townhouse|Resort|Lodge)\\b/i);
                results.push({
                    title, property_id: idm?idm[1]:null, url,
                    price_per_night: pm?parseInt(pm[1].replace(/,/g,'')):null,
                    rating: rm?parseFloat(rm[1]):null,
                    reviews_count: rvm?parseInt(rvm[1].replace(/,/g,'')):null,
                    bedrooms: bm?parseInt(bm[1]):null,
                    bathrooms: btm?parseFloat(btm[1]):null,
                    sleeps: sm?parseInt(sm[1]):null,
                    property_type: tm?tm[1]:null,
                    image_url: img?(img.getAttribute('src')||img.getAttribute('data-src')):null,
                });
            } catch(e) {}
        }

        // Fallback: repeated siblings with $
        if (results.length===0) {
            const pm = new Map();
            for (const el of document.querySelectorAll('div,article,li')) {
                const t=el.innerText||'';
                if (!t.includes('$') || t.length<20 || t.length>5000) continue;
                const p=el.parentElement; if(!p) continue;
                if (!pm.has(p)) pm.set(p,[]);
                pm.get(p).push(el);
            }
            let best=[];
            for (const [,ch] of pm) if (ch.length>best.length && ch.length>=3) best=ch;
            for (const el of best) {
                const t=el.innerText||'';
                const la=el.querySelector('a[href]');
                const h=la?(la.getAttribute('href')||''):'';
                const url=h.startsWith('http')?h:(h?'https://www.vrbo.com'+h:null);
                const tEl=el.querySelector('h1,h2,h3,h4');
                const title=tEl?tEl.innerText.trim():(t.split('\\n').find(l=>l.trim().length>5)||'').trim();
                if(!title) continue;
                const pm2=t.match(/\\$(\\d[\\d,]*)/);
                const img=el.querySelector('img[src*="http"]');
                results.push({
                    title, property_id:null, url,
                    price_per_night: pm2?parseInt(pm2[1].replace(/,/g,'')):null,
                    rating:null, reviews_count:null, bedrooms:null, bathrooms:null,
                    sleeps:null, property_type:null,
                    image_url: img?img.getAttribute('src'):null,
                });
            }
        }
        return results;
    }
    """)


# ── Detail page ─────────────────────────────────────────────────────────────

async def extract_property_details(page):
    await page.wait_for_load_state("domcontentloaded")
    await human_scroll(page, 5); await human_delay(1.5, 3)
    return await page.evaluate("""
    () => {
        const txt=s=>{const e=document.querySelector(s);return e?e.innerText.trim():null};
        const title=txt('h1');
        let desc=null;
        document.querySelectorAll('p,[class*="description" i]').forEach(e=>{
            const t=e.innerText.trim();if(t.length>(desc||'').length&&t.length>50)desc=t;
        });
        const host=txt('[class*="host" i]');
        const loc=txt('address,[class*="location" i]');
        const am=[];
        document.querySelectorAll('[class*="amenity" i] li,[class*="amenity" i] span').forEach(e=>{
            const a=e.innerText.trim(); if(a&&a.length>1&&a.length<80)am.push(a);
        });
        const imgs=[];
        document.querySelectorAll('img[src*="http"]').forEach(i=>{
            const s=i.getAttribute('src')||'';
            if(s&&!s.includes('pixel')&&!s.includes('beacon')&&!s.includes('.svg'))imgs.push(s);
        });
        return {title,description:desc?desc.substring(0,2000):null,host,location:loc,
                amenities:[...new Set(am)],images:[...new Set(imgs)].slice(0,20)};
    }
    """)


# ── Pagination ──────────────────────────────────────────────────────────────

async def try_next_page(page):
    for sel in [
        'a[aria-label*="Next" i]','button[aria-label*="Next" i]',
        'a[data-stid="search-results-next"]','a[rel="next"]',
        '[class*="pagination" i] a:last-child',
    ]:
        btn = await page.query_selector(sel)
        if btn:
            dis = await btn.evaluate("e=>e.disabled||e.getAttribute('aria-disabled')==='true'")
            if dis: continue
            log.info("  next page via %s", sel)
            try:
                await btn.click(); await human_delay(3,5); return True
            except: pass
    # URL param fallback
    u = urlparse(page.url); q = parse_qs(u.query)
    for pn in ["page","pn","pageNumber"]:
        cur = int(q.get(pn,[1])[0])
        q2 = {k:v[0] for k,v in q.items()}; q2[pn]=str(cur+1)
        nu = u._replace(query=ue(q2)).geturl()
        log.info("  next page via URL param %s=%d", pn, cur+1)
        await page.goto(nu, wait_until="domcontentloaded", timeout=60000)
        await human_delay(2,4); return True
    return False


def dedupe(listings):
    seen=set(); out=[]
    for i in listings:
        k=i.get("property_id") or i.get("url") or i.get("title","")
        if k and k not in seen: seen.add(k); out.append(i)
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  SEARCH VIA FORM: type destination into homepage search box like a real user
# ══════════════════════════════════════════════════════════════════════════════

async def search_via_form(page, destination, checkin=None, checkout=None) -> bool:
    """
    Use the homepage search form instead of navigating directly to /search URL.
    Akamai's behavioral analysis is less likely to flag this as bot traffic.
    """
    log.info("  looking for search input on homepage …")

    # VRBO uses various search input selectors across redesigns
    search_input_selectors = [
        'input[data-stid="destination_form_field"]',
        'input[placeholder*="destination" i]',
        'input[placeholder*="where" i]',
        'input[placeholder*="going" i]',
        'input[aria-label*="destination" i]',
        'input[aria-label*="where" i]',
        'input[aria-label*="going" i]',
        'input[aria-label*="search" i]',
        'input[name*="destination" i]',
        'input[name*="location" i]',
        'input[id*="destination" i]',
        'input[id*="location" i]',
        'button[aria-label*="destination" i]',
        'button[aria-label*="where" i]',
        '[data-testid*="destination"] input',
        '[data-testid*="search"] input',
        '[class*="destination"] input',
        '[class*="search-form"] input',
        '[class*="SearchForm"] input',
    ]

    search_input = None
    for sel in search_input_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                search_input = el
                log.info("  found search input: %s", sel)
                break
        except:
            pass

    if not search_input:
        # Try clicking any element that looks like a search trigger
        trigger_selectors = [
            'button:has-text("Search")',
            'button:has-text("Where")',
            'button:has-text("Destination")',
            '[class*="search" i] button:first-child',
            '[data-testid*="search-button"]',
        ]
        for sel in trigger_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await human_delay(1, 2)
                    # Now try to find the input again
                    for isel in search_input_selectors:
                        el = await page.query_selector(isel)
                        if el and await el.is_visible():
                            search_input = el
                            log.info("  found search input after trigger: %s", isel)
                            break
                    if search_input:
                        break
            except:
                pass

    if not search_input:
        log.warning("  could not find search input on homepage")
        return False

    # Clear any existing text and type destination with human-like speed
    try:
        await search_input.click()
        await human_delay(0.3, 0.6)
        await page.keyboard.press("Control+a")
        await human_delay(0.1, 0.3)

        # Type character by character
        for ch in destination:
            await page.keyboard.type(ch, delay=random.randint(60, 180))
            if random.random() < 0.03:
                await asyncio.sleep(random.uniform(0.3, 0.7))

        log.info("  typed destination: %s", destination)
        await human_delay(1.5, 3.0)  # Wait for autocomplete

    except Exception as e:
        log.warning("  failed to type in search input: %s", e)
        return False

    # Try to click the first autocomplete suggestion
    suggestion_selectors = [
        '[class*="suggestion" i] li:first-child',
        '[class*="autocomplete" i] li:first-child',
        '[class*="Autocomplete" i] li:first-child',
        '[data-stid="destination_form_field-result-item"]',
        '[data-testid*="suggestion"]:first-child',
        '[role="listbox"] [role="option"]:first-child',
        '[role="listbox"] li:first-child',
        'ul[class*="result" i] li:first-child',
        'ul[class*="suggest" i] li:first-child',
        '[class*="dropdown" i] li:first-child',
        '[id*="suggestion"] li:first-child',
    ]

    suggestion_clicked = False
    for sel in suggestion_selectors:
        try:
            sug = await page.query_selector(sel)
            if sug and await sug.is_visible():
                await human_delay(0.3, 0.7)
                await sug.click()
                log.info("  clicked autocomplete suggestion: %s", sel)
                suggestion_clicked = True
                break
        except:
            pass

    if not suggestion_clicked:
        log.info("  no autocomplete found, pressing Enter")
        await page.keyboard.press("Enter")

    await human_delay(1, 2)

    # Optionally set dates (if date inputs are visible)
    if checkin or checkout:
        log.info("  setting dates: %s → %s", checkin, checkout)
        # Date inputs are complex; for now just add to URL params later
        # Most VRBO searches work without dates initially

    # Click the search button
    search_btn_selectors = [
        'button[data-stid="apply-date-selector"]',
        'button[type="submit"]',
        'button:has-text("Search")',
        'button:has-text("Find")',
        'button[aria-label*="search" i]',
        'button[data-testid*="submit"]',
        'button[data-testid*="search"]',
        '[class*="search" i] button[type="submit"]',
        'form button:last-of-type',
    ]

    for sel in search_btn_selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await human_delay(0.3, 0.7)
                await btn.click()
                log.info("  clicked search button: %s", sel)
                break
        except:
            pass

    # Wait for navigation / results to load
    log.info("  waiting for search results to load …")
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
    except:
        pass
    await human_delay(3, 5)

    # Verify we landed on a search results page
    final_url = page.url
    log.info("  final URL: %s", final_url)

    if "/search" in final_url or "destination" in final_url or "results" in final_url:
        log.info("  ✓ search navigation successful via form")

        # If dates were requested, try adding them to the URL
        if (checkin or checkout) and "startDate" not in final_url:
            from urllib.parse import urlparse as up, parse_qs as pq, urlencode as uen
            parsed = up(final_url)
            qparams = {k: v[0] for k, v in pq(parsed.query).items()}
            if checkin:  qparams["startDate"] = checkin
            if checkout: qparams["endDate"]   = checkout
            new_url = parsed._replace(query=uen(qparams)).geturl()
            log.info("  adding dates to URL: %s", new_url)
            try:
                await page.goto(new_url, wait_until="domcontentloaded", timeout=60000)
                await human_delay(3, 5)
            except:
                log.warning("  date URL navigation failed, proceeding without dates")
        return True

    # Maybe we're still on the homepage — check if URL changed at all
    if final_url == BASE_URL or final_url == BASE_URL + "/":
        log.warning("  search form did not navigate away from homepage")
        return False

    # URL changed to something else — might still be search results
    log.info("  navigated to: %s — proceeding", final_url)
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  PROXY URL PARSER
# ══════════════════════════════════════════════════════════════════════════════

def parse_proxy_url(proxy_url: str) -> dict:
    """
    Parse proxy URL into Playwright's expected format.

    Input formats:
      http://user:pass@host:port
      http://host:port
      socks5://user:pass@host:port
      host:port

    Returns:
      {"server": "http://host:port", "username": "user", "password": "pass"}
    """
    proxy_url = proxy_url.strip()

    # Add scheme if missing
    if "://" not in proxy_url:
        proxy_url = "http://" + proxy_url

    parsed = urlparse(proxy_url)

    # Build server URL without credentials
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "localhost"
    port = parsed.port

    if port:
        server = f"{scheme}://{host}:{port}"
    else:
        server = f"{scheme}://{host}"

    result = {"server": server}

    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password

    return result


# ══════════════════════════════════════════════════════════════════════════════
#  WARM-UP: visit homepage first to get cookies & pass Akamai checks
# ══════════════════════════════════════════════════════════════════════════════

async def warmup(page, solver):
    """Visit VRBO homepage, accept cookies, build session — before doing the search."""
    log.info("Warm-up: visiting homepage to establish session …")

    try:
        resp = await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        err_str = str(exc)
        if "ERR_PROXY_CONNECTION_FAILED" in err_str or "PROXY" in err_str.upper():
            log.error("⚠  Proxy connection failed!")
            log.error("   Check your proxy URL format: http://user:pass@host:port")
            log.error("   Make sure the proxy server is running and credentials are correct.")
            log.error("   Get working proxies at https://2prx.com")
        elif "ERR_CONNECTION" in err_str or "TIMEOUT" in err_str.upper():
            log.error("⚠  Connection to vrbo.com failed: %s", err_str[:120])
            log.error("   Check your internet connection and proxy settings.")
        else:
            log.error("⚠  Navigation failed: %s", err_str[:200])
        return False

    status = resp.status if resp else "?"
    log.info("Homepage status: %s | URL: %s", status, page.url)

    if status == 403:
        log.warning("⚠  Homepage returned 403 — Akamai is blocking this IP.")
        log.warning("   → Use a residential proxy: --proxy http://user:pass@gate.2prx.com:8080")
        log.warning("   → Or try with --headed mode (non-headless)")
        return False

    # Wait for page to load
    await human_delay(3, 5)

    # Accept cookie consent if present
    for sel in [
        'button[id*="accept" i]', 'button[class*="accept" i]',
        'button[data-testid*="accept" i]', 'button:has-text("Accept")',
        'button:has-text("Accept All")', 'button:has-text("I Accept")',
        'button:has-text("OK")', 'button:has-text("Got it")',
        '#onetrust-accept-btn-handler',
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                log.info("  accepted cookies via: %s", sel)
                await human_delay(1, 2)
                break
        except:
            pass

    # CAPTCHA on homepage?
    if solver.enabled:
        await solver.detect_and_solve(page)

    # Random mouse movements to look human
    await random_mouse_move(page)
    await human_delay(1, 2)
    await human_scroll(page, 2)
    await human_delay(1, 2)

    log.info("Warm-up complete ✓")
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN SCRAPER
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_vrbo(
    destination, checkin=None, checkout=None, max_pages=5, max_properties=0,
    detail_pages=False, output_format="json", output_file=None,
    proxy=None, captcha_key=None, headless=True, debug=False,
):
    solver = CaptchaSolver(captcha_key)
    all_listings = []

    async with async_playwright() as pw:
        # ── Launch browser ──────────────────────────────────────────────
        #
        # KEY: use channel="chrome" to launch REAL Chrome (not Chromium).
        #      Chromium has a different TLS fingerprint that Akamai detects.
        #      Fall back to "chromium" if Chrome is not installed.
        #
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-component-update",
            "--no-service-autorun",
        ]

        proxy_cfg = None
        if proxy:
            proxy_cfg = parse_proxy_url(proxy)
            log.info("Using proxy: %s", proxy_cfg["server"])

        browser = None
        for channel in ["chrome", "msedge", "chromium"]:
            try:
                log.info("Trying browser channel: %s …", channel)
                kw = {
                    "headless": headless,
                    "args": launch_args,
                    "channel": channel,
                }
                if proxy_cfg:
                    kw["proxy"] = proxy_cfg
                browser = await pw.chromium.launch(**kw)
                log.info("Launched: %s ✓", channel)
                break
            except Exception as e:
                log.warning("Channel '%s' not available: %s", channel, str(e)[:80])
                continue

        if not browser:
            log.error("No browser available. Install Chrome: playwright install chrome")
            return []

        # ── Create context ──────────────────────────────────────────────
        viewport = random.choice(VIEWPORTS)
        ua = random.choice(USER_AGENTS)
        ctx = await browser.new_context(
            viewport=viewport,
            user_agent=ua,
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Sec-CH-UA": '"Google Chrome";v="126", "Chromium";v="126", "Not/A)Brand";v="8"',
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"' + random.choice(["Windows", "macOS", "Linux"]) + '"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
            color_scheme="light",
        )

        page = await ctx.new_page()
        await page.add_init_script(build_stealth_script())

        # ── API interceptor ─────────────────────────────────────────────
        interceptor = APIInterceptor()
        page.on("response", interceptor.handle)

        # ── Warm-up phase ───────────────────────────────────────────────
        warmup_ok = await warmup(page, solver)
        if not warmup_ok:
            if debug:
                try: await page.screenshot(path="vrbo_debug_warmup.png")
                except: pass
                log.info("Debug screenshot → vrbo_debug_warmup.png")
            await browser.close()
            log.error("Homepage blocked. Your proxy IP is not passing Akamai.")
            log.error("Solution: use a US residential proxy from 2prx.com")
            return []

        # ── Navigate to search ──────────────────────────────────────────
        #
        # KEY: Do NOT navigate directly to /search?destination=... — Akamai
        # flags that as bot behavior. Instead, use the search form on the
        # homepage like a real user would.
        #
        log.info("Searching via homepage form: %s", destination)
        search_ok = await search_via_form(page, destination, checkin, checkout)

        if not search_ok:
            # Fallback: try direct URL with error handling
            log.info("Form search failed — falling back to direct URL …")
            params = {"destination": destination}
            if checkin:  params["startDate"] = checkin
            if checkout: params["endDate"]   = checkout
            search_url = f"{BASE_URL}/search?{urlencode(params)}"
            log.info("Navigating to: %s", search_url)
            try:
                resp = await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
                status = resp.status if resp else "?"
            except Exception as exc:
                # ERR_HTTP_RESPONSE_CODE_FAILURE means we got a response (e.g. 403)
                # but Playwright raised because it's non-2xx. Page might still have content.
                err_str = str(exc)
                if "ERR_HTTP_RESPONSE_CODE_FAILURE" in err_str:
                    log.warning("Got non-2xx response, checking page content …")
                    status = "non-2xx"
                else:
                    log.error("⚠  Navigation failed: %s", err_str[:150])
                    await browser.close()
                    return []

            log.info("Search status: %s | URL: %s", status, page.url)

            if status == 403 or (isinstance(status, str) and "non-2xx" in status):
                try:
                    body = await page.evaluate("document.body.innerText.substring(0,200)")
                except Exception:
                    body = "Access Denied (page context unavailable)"
                if "Access Denied" in body or "403" in body or "non-2xx" in str(status):
                    log.error("⚠  Blocked by Akamai (403 Access Denied).")
                    log.error("   Your proxy IP is blacklisted. You need a DIFFERENT proxy.")
                    log.error("   Requirements:")
                    log.error("   • Type: Residential (not datacenter)")
                    log.error("   • Country: United States")
                    log.error("   • Provider: 2prx.com (select US residential pool)")
                    log.error("   Also try: --headed mode, Anti-Detect Browser (2captcha.com/anti-detect-browser)")
                    if debug:
                        try: await page.screenshot(path="vrbo_debug_403.png")
                        except: pass
                    await browser.close()
                    return []

        # Wait for JS rendering
        log.info("Waiting for page rendering …")
        await human_delay(5, 8)
        await human_scroll(page, 4)
        await human_delay(2, 3)

        if solver.enabled:
            await solver.detect_and_solve(page)
            await human_delay(1, 2)

        if debug:
            await page.screenshot(path="vrbo_debug.png", full_page=False)
            log.info("Debug screenshot → vrbo_debug.png")
            log.info("Title: %s", await page.title())
            body_start = await page.evaluate("document.body.innerText.substring(0,300)")
            log.info("Body[0:300]: %s", repr(body_start))

        # ── Paginate ────────────────────────────────────────────────────
        pages_done = 0
        while pages_done < max_pages:
            pages_done += 1
            log.info("--- Page %d / %d ---", pages_done, max_pages)

            await human_scroll(page, 4)
            await human_delay(2, 3)

            s1 = interceptor.extract()
            s2 = await extract_embedded_json(page)
            s3 = await extract_listings_dom(page)
            page_list = dedupe(s1 + s2 + s3)

            if not page_list:
                log.warning("No listings found (api=%d emb=%d dom=%d).", len(s1),len(s2),len(s3))
                log.warning("Tips: use --proxy, --headed, --debug")
                if debug:
                    await page.screenshot(path=f"vrbo_debug_p{pages_done}.png")
                    bt = await page.evaluate("document.body.innerText.substring(0,500)")
                    log.info("Body text: %s", repr(bt))
                break

            log.info("Found %d listings (api=%d emb=%d dom=%d)", len(page_list),len(s1),len(s2),len(s3))

            if detail_pages:
                for i, lst in enumerate(page_list):
                    if lst.get("url"):
                        log.info("  → detail %d/%d: %s", i+1, len(page_list), (lst.get("title",""))[:50])
                        dp = await ctx.new_page()
                        await dp.add_init_script(build_stealth_script())
                        try:
                            await dp.goto(lst["url"], wait_until="domcontentloaded", timeout=45000)
                            await human_delay(1.5, 3)
                            if solver.enabled: await solver.detect_and_solve(dp)
                            det = await extract_property_details(dp)
                            lst.update({k:v for k,v in det.items() if v})
                        except Exception as e:
                            log.warning("Detail error: %s", e)
                        finally:
                            await dp.close()
                        await human_delay(1, 2.5)

            all_listings.extend(page_list)
            log.info("Total: %d", len(all_listings))

            if max_properties and len(all_listings) >= max_properties:
                all_listings = all_listings[:max_properties]; break

            if pages_done < max_pages:
                await random_mouse_move(page)
                if not await try_next_page(page):
                    log.info("No more pages."); break
                if solver.enabled: await solver.detect_and_solve(page)

        await browser.close()

    # ── Output ──────────────────────────────────────────────────────────
    ts = datetime.utcnow().isoformat()+"Z"
    for i in all_listings: i["scraped_at"]=ts; i["source"]="vrbo.com"

    if not output_file:
        sd = re.sub(r"[^a-zA-Z0-9]+","_",destination).strip("_").lower()
        output_file = f"vrbo_{sd}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    if output_format == "csv":
        fp = output_file if output_file.endswith(".csv") else output_file+".csv"
        if all_listings:
            keys = list(dict.fromkeys(k for i in all_listings for k in i))
            with open(fp,"w",newline="",encoding="utf-8") as f:
                w = csv.DictWriter(f,fieldnames=keys,extrasaction="ignore")
                w.writeheader(); w.writerows(all_listings)
        log.info("Saved %d → %s", len(all_listings), fp)
    else:
        fp = output_file if output_file.endswith(".json") else output_file+".json"
        with open(fp,"w",encoding="utf-8") as f:
            json.dump(all_listings, f, indent=2, ensure_ascii=False)
        log.info("Saved %d → %s", len(all_listings), fp)

    return all_listings


def main():
    p = argparse.ArgumentParser(
        description="VRBO Scraper (Playwright) — vacation rental data extraction",
        epilog="Tip: Install Chrome for best results: playwright install chrome"
    )
    p.add_argument("--destination","-d", required=True, help='e.g. "Orlando, FL"')
    p.add_argument("--checkin", help="YYYY-MM-DD")
    p.add_argument("--checkout", help="YYYY-MM-DD")
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--max-properties", type=int, default=0)
    p.add_argument("--details", action="store_true", help="Scrape individual property pages")
    p.add_argument("--format", choices=["json","csv"], default="json")
    p.add_argument("--output","-o")
    p.add_argument("--proxy", help="Proxy URL (get residential proxies at 2prx.com)")
    p.add_argument("--captcha-key", help="2captcha.com API key")
    p.add_argument("--headed", action="store_true", help="Non-headless mode (visible browser)")
    p.add_argument("--debug", action="store_true", help="Save screenshots & extra logs")
    a = p.parse_args()

    results = asyncio.run(scrape_vrbo(
        destination=a.destination, checkin=a.checkin, checkout=a.checkout,
        max_pages=a.max_pages, max_properties=a.max_properties,
        detail_pages=a.details, output_format=a.format, output_file=a.output,
        proxy=a.proxy, captcha_key=a.captcha_key,
        headless=not a.headed, debug=a.debug,
    ))
    print(f"\nDone — {len(results)} properties scraped.")

if __name__ == "__main__":
    main()
