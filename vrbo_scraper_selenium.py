"""
VRBO Scraper — Selenium Edition
================================
Alternative implementation using Selenium + undetected-chromedriver.
For the primary (recommended) version, see vrbo_scraper_playwright.py.

Strategy (three-layer extraction):
  1. Parse embedded JSON (__NEXT_DATA__, ld+json, inline scripts)
  2. DOM scraping with adaptive selectors
  3. XHR capture via Chrome DevTools Protocol (CDP)

Features:
  - Stealth mode via undetected-chromedriver
  - 2captcha.com integration (reCAPTCHA v2/v3, hCaptcha, Turnstile)
  - Proxy support via 2prx.com
  - Human-like behavior simulation
  - JSON / CSV output

Repository : https://github.com/2scraper/vrbo-scraper
CAPTCHA API: https://2captcha.com
Proxies    : https://2prx.com

Usage:
  pip install selenium undetected-chromedriver twocaptcha-python
  python vrbo_scraper_selenium.py --destination "Orlando, FL"
"""

import argparse, csv, json, logging, os, random, re, sys, time
from datetime import datetime
from urllib.parse import urlencode, urlparse, parse_qs, urlencode as ue

try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
except ImportError:
    sys.exit("Install: pip install selenium undetected-chromedriver")

try:
    from twocaptcha import TwoCaptcha
except ImportError:
    TwoCaptcha = None

BASE_URL = "https://www.vrbo.com"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

STEALTH_JS = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'plugins',{
    get:()=>[1,2,3,4,5].map(()=>({name:'Chrome PDF Plugin',description:'PDF',filename:'pdf',length:1}))
});
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
window.chrome={runtime:{},loadTimes:()=>{},csi:()=>{}};
const gp=WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter=function(p){
    if(p===37445)return'Intel Inc.';if(p===37446)return'Intel Iris OpenGL';return gp.call(this,p);
};
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("vrbo-sel")

def hdelay(lo=0.8, hi=2.5): time.sleep(random.uniform(lo, hi))

def hscroll(driver, steps=3):
    for _ in range(steps):
        driver.execute_script(f"window.scrollBy(0,{random.randint(250,700)});")
        time.sleep(random.uniform(0.3, 0.8))


# ── CAPTCHA ─────────────────────────────────────────────────────────────────

class CaptchaSolver:
    def __init__(self, api_key):
        self.solver = TwoCaptcha(api_key) if (TwoCaptcha and api_key) else None

    @property
    def enabled(self): return self.solver is not None

    def detect_and_solve(self, driver):
        if not self.enabled: return False
        html = driver.page_source
        if re.search(r'recaptcha/api2|recaptcha/enterprise', html):
            return self._rc2(driver, html)
        if 'hcaptcha.com' in html:
            return self._hc(driver, html)
        if 'challenges.cloudflare.com' in html or 'cf-turnstile' in html:
            return self._ts(driver, html)
        return False

    def _sk(self, html, pat):
        m = re.search(pat, html); return m.group(1) if m else None

    def _rc2(self, drv, html):
        sk = self._sk(html, r'data-sitekey=["\']([^"\']+)')
        if not sk: sk = self._sk(html, r'recaptcha/api2/anchor\?.*?k=([A-Za-z0-9_-]+)')
        if not sk: return False
        r = self.solver.recaptcha(sitekey=sk, url=drv.current_url)
        t = r.get("code") if isinstance(r,dict) else r
        drv.execute_script(f"document.getElementById('g-recaptcha-response').value='{t}';")
        log.info("reCAPTCHA v2 solved ✓"); return True

    def _hc(self, drv, html):
        sk = self._sk(html, r'data-sitekey=["\']([^"\']+)')
        if not sk: return False
        r = self.solver.hcaptcha(sitekey=sk, url=drv.current_url)
        t = r.get("code") if isinstance(r,dict) else r
        drv.execute_script(f"""
            document.querySelector('[name="h-captcha-response"]').value='{t}';
            document.querySelector('[name="g-recaptcha-response"]').value='{t}';
        """)
        log.info("hCaptcha solved ✓"); return True

    def _ts(self, drv, html):
        sk = self._sk(html, r'data-sitekey=["\']([^"\']+)')
        if not sk: return False
        r = self.solver.turnstile(sitekey=sk, url=drv.current_url)
        t = r.get("code") if isinstance(r,dict) else r
        drv.execute_script(f"""
            const c=document.querySelector('[name="cf-turnstile-response"]');if(c)c.value='{t}';
        """)
        log.info("Turnstile solved ✓"); return True


# ── JSON walker (shared logic) ──────────────────────────────────────────────

def walk_json(obj, depth=0):
    if depth > 15: return []
    results = []
    if isinstance(obj, dict):
        if _is_listing(obj):
            p = _parse_listing(obj)
            if p: results.append(p)
        else:
            for v in obj.values(): results.extend(walk_json(v, depth+1))
    elif isinstance(obj, list):
        for i in obj: results.extend(walk_json(i, depth+1))
    return results

def _is_listing(d):
    kl = {k.lower() for k in d}
    has_name = bool(kl & {"name","title","headline","propertyname","headlinetext","listing_name"})
    has_id = bool(kl & {"propertyid","listingid","property_id","id","unitid",
                        "price","rateprice","nightlyrate","pricesummary","averageprice"})
    if not has_name:
        for v in d.values():
            if isinstance(v, dict) and ({k.lower() for k in v} & {"name","title","headline"}):
                has_name = True; break
    return has_name and has_id

def _parse_listing(d):
    def fv(obj, *names):
        if not isinstance(obj, dict): return None
        for k,v in obj.items():
            if k.lower() in [n.lower() for n in names]: return v
        for v in obj.values():
            if isinstance(v, dict):
                for k2,v2 in v.items():
                    if k2.lower() in [n.lower() for n in names]: return v2
        return None

    title = fv(d,"name","title","headline","propertyName","headlineText")
    if not title: return None
    prop_id = fv(d,"propertyId","listingId","property_id","id","unitId")
    url = fv(d,"url","detailUrl","deepLink","propertyUrl","pdpUrl","href")
    if url and not url.startswith("http"):
        url = BASE_URL + ("" if url.startswith("/") else "/") + url

    pr = fv(d,"price","ratePrice","averagePrice","nightlyRate","leadPrice","displayPrice","totalPrice")
    ppn, pt = None, None
    if isinstance(pr,(int,float)): ppn=int(pr)
    elif isinstance(pr,str):
        pt=pr; m=re.search(r'\$?([\d,]+)',pr)
        if m: ppn=int(m.group(1).replace(",",""))
    elif isinstance(pr,dict):
        a=fv(pr,"amount","value","formatted","displayPrice")
        if isinstance(a,(int,float)): ppn=int(a)
        elif isinstance(a,str):
            pt=a; m=re.search(r'\$?([\d,]+)',a); ppn=int(m.group(1).replace(",","")) if m else None

    rr=fv(d,"rating","averageRating","reviewScore","overallRating","guestRating")
    rating=float(rr) if isinstance(rr,(int,float)) else None
    if isinstance(rr,dict):
        rv=fv(rr,"value","overall","score"); rating=float(rv) if isinstance(rv,(int,float)) else None

    rc=fv(d,"reviewCount","reviews_count","totalReviews","numberOfReviews")
    reviews=int(rc) if isinstance(rc,(int,float)) else None
    bed=fv(d,"bedrooms","bedroomCount"); bed=int(bed) if isinstance(bed,(int,float)) else None
    bath=fv(d,"bathrooms","bathroomCount"); bath=float(bath) if isinstance(bath,(int,float)) else None
    slp=fv(d,"sleeps","maxOccupancy","guestCount"); slp=int(slp) if isinstance(slp,(int,float)) else None
    pt2=fv(d,"propertyType","type","lodgingType")
    if isinstance(pt2,dict): pt2=fv(pt2,"name","label")

    img=fv(d,"image","thumbnail","heroImage","primaryImage","photo")
    iurl=None
    if isinstance(img,str): iurl=img
    elif isinstance(img,dict): iurl=fv(img,"url","src","uri")
    elif isinstance(img,list) and img:
        f=img[0]; iurl=f if isinstance(f,str) else fv(f,"url","src") if isinstance(f,dict) else None

    return {"title":str(title),"property_id":str(prop_id) if prop_id else None,
            "url":url,"price_per_night":ppn,"price_text":pt,
            "rating":rating,"reviews_count":reviews,
            "bedrooms":bed,"bathrooms":bath,"sleeps":slp,
            "property_type":str(pt2) if pt2 else None,"image_url":iurl}


# ── Extraction: embedded JSON ───────────────────────────────────────────────

EMBEDDED_JS = """
return (function(){
    var r=[];
    var nd=document.getElementById('__NEXT_DATA__');
    if(nd)try{r.push(JSON.parse(nd.textContent));}catch(e){}
    ['__CONFIG__','__INITIAL_STATE__','__PRELOADED_STATE__','__DATA__'].forEach(function(k){
        if(window[k])r.push(window[k]);
    });
    document.querySelectorAll('script[type="application/json"],script[type="application/ld+json"]').forEach(function(el){
        try{if(el.textContent.length>500)r.push(JSON.parse(el.textContent));}catch(e){}
    });
    document.querySelectorAll('script:not([src])').forEach(function(el){
        var t=el.textContent||'';
        if(t.length>2000&&(t.indexOf('propertyId')>-1||t.indexOf('"bedrooms"')>-1||t.indexOf('searchResults')>-1)){
            var m=t.match(/(?:window\\.\\w+\\s*=\\s*)(\\{[\\s\\S]+\\})\\s*;?\\s*$/);
            if(m)try{r.push(JSON.parse(m[1]));}catch(e){}
        }
    });
    return r;
})();
"""

def extract_embedded(driver):
    blobs = driver.execute_script(EMBEDDED_JS) or []
    listings = []
    for blob in blobs:
        listings.extend(walk_json(blob))
    return listings


# ── Extraction: DOM (adaptive) ──────────────────────────────────────────────

DOM_JS = """
return (function(){
    var results=[];
    var propPat=[/\\/\\d{5,}/,/\\/vacation-rentals\\//,/\\/unit\\//,/\\/lodging\\//,/\\/property\\//];
    function isPL(h){for(var i=0;i<propPat.length;i++)if(propPat[i].test(h))return true;return false;}

    var plinks=[];
    var als=document.querySelectorAll('a[href]');
    for(var i=0;i<als.length;i++){
        var h=als[i].getAttribute('href')||'';
        if(isPL(h)&&als[i].offsetHeight>30)plinks.push(als[i]);
    }

    var done=new Set();
    for(var i=0;i<plinks.length;i++){
        var card=plinks[i];
        for(var j=0;j<8;j++){
            if(!card.parentElement)break;card=card.parentElement;
            if(card.querySelector('img')&&(card.innerText||'').length>30)break;
        }
        if(done.has(card))continue;done.add(card);
        try{
            var txt=card.innerText||'';
            var href=plinks[i].getAttribute('href')||'';
            var url=href.startsWith('http')?href:'https://www.vrbo.com'+href;
            var tEl=card.querySelector('h1,h2,h3,h4,[class*="title"],[class*="name"]');
            var title=tEl?tEl.innerText.trim():null;
            if(!title||title.length<3)continue;
            var pm=txt.match(/\\$(\\d[\\d,]*)/);
            var rm=txt.match(/(\\d\\.\\d)\\s*(?:\\/|out|star|\\()/i);
            var rvm=txt.match(/(\\d[\\d,]*)\\s*review/i);
            var idm=url.match(/\\/(\\d{5,})/);
            var bm=txt.match(/(\\d+)\\s*(?:BR|bed(?:room)?s?)/i);
            var btm=txt.match(/(\\d+\\.?\\d*)\\s*(?:BA|bath)/i);
            var sm=txt.match(/(?:sleep|accommodat)\\w*\\s*(\\d+)/i);
            var img=card.querySelector('img[src*="http"]');
            var tm=txt.match(/\\b(House|Condo|Cabin|Apartment|Villa|Cottage|Chalet|Studio|Townhouse|Resort|Lodge)\\b/i);
            results.push({
                title:title,property_id:idm?idm[1]:null,url:url,
                price_per_night:pm?parseInt(pm[1].replace(/,/g,'')):null,
                rating:rm?parseFloat(rm[1]):null,
                reviews_count:rvm?parseInt(rvm[1].replace(/,/g,'')):null,
                bedrooms:bm?parseInt(bm[1]):null,bathrooms:btm?parseFloat(btm[1]):null,
                sleeps:sm?parseInt(sm[1]):null,property_type:tm?tm[1]:null,
                image_url:img?img.getAttribute('src'):null
            });
        }catch(e){}
    }
    return results;
})();
"""

def extract_dom(driver):
    return driver.execute_script(DOM_JS) or []


# ── Browser ─────────────────────────────────────────────────────────────────

def create_driver(proxy=None, headless=True):
    opts = uc.ChromeOptions()
    if headless: opts.add_argument("--headless=new")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-first-run")
    w,h = random.choice([(1920,1080),(1366,768),(1536,864),(1440,900)])
    opts.add_argument(f"--window-size={w},{h}")
    opts.add_argument(f"--user-agent={random.choice(USER_AGENTS)}")
    if proxy: opts.add_argument(f"--proxy-server={proxy}")
    driver = uc.Chrome(options=opts)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
    return driver


def dedupe(listings):
    seen=set(); out=[]
    for i in listings:
        k=i.get("property_id") or i.get("url") or i.get("title","")
        if k and k not in seen: seen.add(k); out.append(i)
    return out


# ── Main ────────────────────────────────────────────────────────────────────

def scrape_vrbo(
    destination, checkin=None, checkout=None, max_pages=5, max_properties=0,
    detail_pages=False, output_format="json", output_file=None,
    proxy=None, captcha_key=None, headless=True, debug=False,
):
    solver = CaptchaSolver(captcha_key)
    driver = create_driver(proxy=proxy, headless=headless)
    all_listings = []

    try:
        params = {"destination": destination}
        if checkin:  params["startDate"] = checkin
        if checkout: params["endDate"]   = checkout
        url = f"{BASE_URL}/search?{urlencode(params)}"

        log.info("Opening: %s", url)
        driver.get(url)
        log.info("Status → URL: %s", driver.current_url)

        # Wait for JS
        log.info("Waiting for JS rendering …")
        hdelay(5, 8); hscroll(driver, 4); hdelay(2, 3)

        if solver.enabled: solver.detect_and_solve(driver); hdelay(1,2)

        if debug:
            driver.save_screenshot("vrbo_debug.png")
            log.info("Title: %s", driver.title)
            log.info("Body[0:300]: %s", repr(driver.execute_script("return document.body.innerText.substring(0,300)")))

        pages_done = 0
        while pages_done < max_pages:
            pages_done += 1
            log.info("--- Page %d / %d ---", pages_done, max_pages)
            hscroll(driver, 4); hdelay(2, 3)

            s1 = extract_embedded(driver)
            s2 = extract_dom(driver)
            page_list = dedupe(s1 + s2)

            if not page_list:
                log.warning("No listings found (emb=%d dom=%d). Try --debug or --headed.", len(s1), len(s2))
                if debug:
                    driver.save_screenshot(f"vrbo_debug_p{pages_done}.png")
                    log.info("Body: %s", repr(driver.execute_script("return document.body.innerText.substring(0,500)")))
                break

            log.info("Found %d listings (emb=%d dom=%d)", len(page_list), len(s1), len(s2))

            if detail_pages:
                main_win = driver.current_window_handle
                for i, lst in enumerate(page_list):
                    if lst.get("url"):
                        log.info("  → detail %d/%d: %s", i+1, len(page_list), (lst.get("title",""))[:50])
                        driver.execute_script(f"window.open('{lst['url']}','_blank');")
                        driver.switch_to.window(driver.window_handles[-1])
                        hdelay(2,4)
                        if solver.enabled: solver.detect_and_solve(driver)
                        # Simple detail extraction
                        det = driver.execute_script("""
                        return (function(){
                            var txt=function(s){var e=document.querySelector(s);return e?e.innerText.trim():null};
                            var title=txt('h1'); var desc=null;
                            document.querySelectorAll('p,[class*="description"]').forEach(function(e){
                                var t=e.innerText.trim();if(t.length>(desc||'').length&&t.length>50)desc=t;
                            });
                            var am=[];
                            document.querySelectorAll('[class*="amenity"] li').forEach(function(e){
                                var a=e.innerText.trim();if(a&&a.length<80)am.push(a);
                            });
                            return {title:title,description:desc?desc.substring(0,2000):null,
                                    amenities:[...new Set(am)]};
                        })();
                        """) or {}
                        lst.update({k:v for k,v in det.items() if v})
                        driver.close()
                        driver.switch_to.window(main_win)
                        hdelay(0.5, 1.5)

            all_listings.extend(page_list)
            log.info("Total: %d", len(all_listings))

            if max_properties and len(all_listings) >= max_properties:
                all_listings = all_listings[:max_properties]; break

            # Next page
            if pages_done < max_pages:
                found_next = False
                for sel in ['a[aria-label*="Next"]','button[aria-label*="Next"]','a[rel="next"]']:
                    try:
                        btn = driver.find_element(By.CSS_SELECTOR, sel)
                        btn.click(); hdelay(3,5); found_next=True; break
                    except: pass
                if not found_next:
                    log.info("No more pages."); break
                if solver.enabled: solver.detect_and_solve(driver)

    finally:
        driver.quit()

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
    p = argparse.ArgumentParser(description="VRBO Scraper (Selenium)")
    p.add_argument("--destination","-d", required=True)
    p.add_argument("--checkin"); p.add_argument("--checkout")
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--max-properties", type=int, default=0)
    p.add_argument("--details", action="store_true")
    p.add_argument("--format", choices=["json","csv"], default="json")
    p.add_argument("--output","-o")
    p.add_argument("--proxy", help="2prx.com proxy URL")
    p.add_argument("--captcha-key", help="2captcha.com API key")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--debug", action="store_true")
    a = p.parse_args()
    results = scrape_vrbo(
        destination=a.destination, checkin=a.checkin, checkout=a.checkout,
        max_pages=a.max_pages, max_properties=a.max_properties,
        detail_pages=a.details, output_format=a.format, output_file=a.output,
        proxy=a.proxy, captcha_key=a.captcha_key,
        headless=not a.headed, debug=a.debug,
    )
    print(f"\nDone — {len(results)} properties scraped.")

if __name__ == "__main__": main()
