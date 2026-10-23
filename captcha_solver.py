"""
captcha_solver.py
------------------
Shared helper used by all three scrapers (Playwright / Selenium / Puppeteer).

Detection runs after EVERY page navigation in the main loop of all three
scrapers, regardless of what URL was requested (category hub, product page,
sign-in, checkout, anything) — this is deliberate, not scoped to any one
page. If Vrbo renders a reCAPTCHA challenge anywhere — account, sign-in
and checkout flows are the usual places — this fires.

**The challenge this site actually serves is DataDome**, through Expedia's
own "Bot or Not?" handler (HTTP 429), which picks its vendor per request and
names the pick in its page config (`whichChallenge`). Every measured refusal
in the README picked DataDome. That has its own path at the bottom of this
file ("DataDome"): a `DataDomeSliderTask` solved through the browser's own
proxy exit and User-Agent, the returned cookie installed with the page's own
cookie attributes, the page re-loaded, and the handler's own validate step
if it is still there. The reCAPTCHA path below is offered a page only when
the handler picks reCAPTCHA.

Detection stays BROAD (which challenge a visitor meets depends on the exit
and on what the address has been doing) while spending stays NARROW:
`--solve-captcha when-blocked` is the default and counts product links
before paying, `page_flow.SOLVES_PER_PAGE` caps a page at one purchase
across every call site, retry and rotation, and `page_flow.STATE_POLICY` —
not this file — decides which state is worth money at all.

reCAPTCHA flow:
  1. Both detectors run and are reconciled (see reconcile_detections) to decide
     the variant: v3, v2-invisible or v2-checkbox. The parameters differ per
     variant and are not interchangeable — v3 params sent for a v2-invisible
     widget buy a token the site rejects.
  2. The challenge goes to 2captcha's API (v2 by default, legacy v1 as a
     one-shot fallback) with the parameters for that variant.
  3. The resulting token is injected into the page's `g-recaptcha-response`
     textarea and any bound callback is invoked.

Often none of this is needed. Over the Scraping Browser API,
`Captcha.setAutoSolve` can clear the challenge inside the browser before this
code gets a turn — treat `Captcha.solveFinished` as the success signal and keep
this path as the fallback rather than assuming every detection completes.
This module is the path for a browser you launched yourself.

No other solving provider is integrated: 2Captcha only.
"""

from __future__ import annotations

import base64
import re
import time
import logging
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode, unquote

import requests

logger = logging.getLogger("captcha_solver")


# An API key must never reach a log, and the easiest way for one to get there
# is an exception message. `requests` puts the FULL URL — query string and all
# — into the text of HTTPError and of every connection error, so any endpoint
# that takes its key as a query parameter leaks it the moment something goes
# wrong. That is not hypothetical: a 400 from the fingerprint endpoint printed
# a live key to the terminal.
#
# Everything raised or logged from this module goes through here first. The
# endpoint and the status survive, because which call failed is the useful
# half and is not the secret.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)

# A PROXY credential is the other secret this module handles, and it does not
# look like a query parameter: it rides in the userinfo of a URL
# (`scheme://login:password@host:port`). Since this module gained a path that
# takes a proxy — a vendor task bound to the solving exit — its
# errors can quote one, and an error that quotes a proxy URL prints the
# password.
#
# Found by a refusal message doing exactly that: a portless proxy was
# rejected with the whole URL in the text. The host and the port survive
# here, deliberately: WHICH exit failed is the useful half of that message
# and is not the secret.
#
# Matched GLOBALLY, not once. A masker that handles the first occurrence
# prints the password the other four times and still looks like it works.
_PROXY_CREDENTIALS_RE = re.compile(
    r"([a-z][a-z0-9+.\-]*://)[^\s/@\"']+:[^\s/@\"']+@", re.IGNORECASE)


def _redact(text) -> str:
    """Everything raised or logged from this module goes through here first.

    An EXCEPTION MESSAGE IS A LOG: `requests` puts the full URL, query string
    and all, into the text of HTTPError and of every connection error, so an
    endpoint that takes its key as a query parameter leaks it the moment
    anything goes wrong.
    """
    masked = _KEY_IN_TEXT_RE.sub(r"\1=***", str(text))
    return _PROXY_CREDENTIALS_RE.sub(r"\1***:***@", masked)

# 2captcha has two generations of solver API and both are live.
#
#   v2 (current, documented at https://2captcha.com/api-docs):
#       POST https://api.2captcha.com/createTask     {clientKey, task:{...}}
#       POST https://api.2captcha.com/getTaskResult  {clientKey, taskId}
#     JSON in, JSON out, typed task objects (RecaptchaV3TaskProxyless etc.).
#
#   v1 (legacy, still accepted):
#       POST https://2captcha.com/in.php   form-encoded, method=userrecaptcha
#       GET  https://2captcha.com/res.php  polling
#
# This module speaks v2 by default and keeps v1 as a fallback, because the
# original build was written against v1 and every live result recorded in the
# README came through it. Switch with solve_recaptcha(..., api_version="v1").
TWOCAPTCHA_API_V2 = "https://api.2captcha.com"
TWOCAPTCHA_CREATE_TASK_URL = f"{TWOCAPTCHA_API_V2}/createTask"
TWOCAPTCHA_GET_RESULT_URL = f"{TWOCAPTCHA_API_V2}/getTaskResult"
TWOCAPTCHA_BALANCE_URL = f"{TWOCAPTCHA_API_V2}/getBalance"

TWOCAPTCHA_IN_URL = "https://2captcha.com/in.php"
TWOCAPTCHA_RES_URL = "https://2captcha.com/res.php"

# v2 rejects an arbitrary minScore: the documented values are these three.
V3_ALLOWED_MIN_SCORES = (0.3, 0.7, 0.9)

@dataclass
class CaptchaChallenge:
    kind: str            # "recaptcha_v3" | "recaptcha_v2_invisible" | "recaptcha_v2"
    sitekey: str
    action: str = "verify"
    page_url: str = ""
    # How the challenge was found: "html" (static markup) or "runtime"
    # (read out of the live page's reCAPTCHA client config). Recorded
    # because the two paths can disagree about the version, and the
    # runtime one is authoritative when they do.
    source: str = "html"
    # Raw `size` from the reCAPTCHA client config, when available:
    # "invisible" for v2-invisible and for v3, absent for a v2 checkbox.
    size: Optional[str] = None

    @property
    def is_v3(self) -> bool:
        return self.kind == "recaptcha_v3"

    @property
    def is_invisible_v2(self) -> bool:
        return self.kind == "recaptcha_v2_invisible"


def detect_recaptcha_v3(html: str, page_url: str) -> Optional[CaptchaChallenge]:
    """Scan raw page HTML for a reCAPTCHA v3 challenge, in either of two
    real-world formats seen so far:

    1. An inline <script> calling grecaptcha.execute('SITEKEY', {action: '...'})
       directly — seen on Kohl's.
    2. A <captcha-widget> custom element carrying the config as HTML
       attributes (data-captcha-type="recaptcha" data-version="v3"
       data-sitekey="..." data-action="..."), with the actual execute()
       call happening inside a bundled JS file, never appearing as
       readable inline script text at all — confirmed live on another
       site's sign-up modal via DevTools inspection (not on Vrbo, whose
       refusal is the "Bot or Not?" page). A previous version of this
       function, which only checked for format 1, reported "no captcha"
       on this exact page despite one being genuinely present — caught by
       manually inspecting the DOM, not by the detector itself, which is
       exactly the gap this format-2 check closes.
    """
    if "recaptcha" not in html.lower():
        return None

    # Format 2 first — a structured HTML attribute match is more reliable
    # than the format-1 regex when both happen to be present.
    for widget_match in re.finditer(r"<captcha-widget\b([^>]*)>", html, re.IGNORECASE):
        attrs = widget_match.group(1)
        version_match = re.search(r'data-version=["\']v(\d)["\']', attrs)
        sitekey_match = re.search(r'data-sitekey=["\']([\w-]{20,})["\']', attrs)
        if version_match and version_match.group(1) == "3" and sitekey_match:
            action_match = re.search(r'data-action=["\']([\w_]+)["\']', attrs)
            action = action_match.group(1) if action_match and action_match.group(1) != "null" else "verify"
            return CaptchaChallenge(
                kind="recaptcha_v3",
                sitekey=sitekey_match.group(1),
                action=action,
                page_url=page_url,
            )

    if "grecaptcha" not in html:
        return None

    exec_match = re.search(
        r"grecaptcha\.execute\(\s*['\"]([\w-]{20,})['\"]\s*,\s*\{\s*action:\s*['\"]([\w_]+)['\"]",
        html,
    )
    if exec_match:
        return CaptchaChallenge(
            kind="recaptcha_v3",
            sitekey=exec_match.group(1),
            action=exec_match.group(2),
            page_url=page_url,
        )

    key_match = re.search(r"data-sitekey=['\"]([\w-]{20,})['\"]", html)
    if key_match and "grecaptcha.render" in html:
        return CaptchaChallenge(kind="recaptcha_v3", sitekey=key_match.group(1), page_url=page_url)

    return None


# ---------------------------------------------------------------------------
# Runtime detection (added 2026-08-24 — the static-HTML detector below no
# longer matched a widget it had been written for, on another site)
# ---------------------------------------------------------------------------
#
# What changed there: the sign-up modal used to render
#   <captcha-widget data-version="v3" data-sitekey="6Leif..." ...>
# with the whole config in HTML attributes. Verified live on 2026-08-24, that
# element is GONE. The modal now loads
#   https://recaptcha.net/recaptcha/api.js?render=explicit
# and configures the widget entirely in JavaScript, into a
#   <div id="register-captcha" class="g-recaptcha">
# container. There is no `data-sitekey`, no inline `grecaptcha.execute(...)`,
# and no `grecaptcha.render` anywhere in the served HTML — all four things
# detect_recaptcha_v3() keys off are absent. It returns None on this page
# even though a real reCAPTCHA is present and active.
#
# The sitekey is still recoverable, but only at runtime, from two places
# that exist in the live page and not in its HTML:
#   1. window.___grecaptcha_cfg.clients — the reCAPTCHA API's own client
#      registry. Property names inside it are minified and change between
#      releases, so this walks the object looking for a value shaped like a
#      sitekey (^6L[\w-]{30,}$) rather than trusting any key name.
#   2. the reCAPTCHA iframe's `k=` query parameter — a stable, documented
#      part of the widget's URL, used as a cross-check and fallback.
#
# The same client config also carries `size`, which is what tells v3 apart
# from v2-invisible. That distinction is not cosmetic: 2captcha needs
# `version=v3` + `action` + `min_score` for one and `invisible=1` for the
# other, and sending the wrong one burns balance for a token that won't
# validate.
RECAPTCHA_DISCOVERY_JS = r"""
() => {
  const out = {found: false, sitekey: null, size: null, action: null,
               enterprise: false, containerId: null, hints: []};

  const SITEKEY_RE = /^6L[\w-]{30,}$/;

  // --- 1. the reCAPTCHA client registry -----------------------------------
  // Minified property names, so match on value shape, not key name.
  try {
    const clients = (window.___grecaptcha_cfg || {}).clients || null;
    if (clients) {
      for (const id of Object.keys(clients)) {
        const seen = new Set();
        const walk = (o, depth) => {
          if (!o || depth > 4 || seen.has(o)) return;
          if (typeof o === 'object') seen.add(o);
          for (const k of Object.keys(o)) {
            let v;
            try { v = o[k]; } catch (e) { continue; }
            if (typeof v === 'string') {
              if (!out.sitekey && SITEKEY_RE.test(v)) { out.sitekey = v; out.found = true; }
              else if (v === 'invisible' || v === 'normal' || v === 'compact') out.size = out.size || v;
            } else if (v && typeof v === 'object') {
              walk(v, depth + 1);
            }
          }
        };
        walk(clients[id], 0);
      }
      if (out.sitekey) out.hints.push('sitekey from ___grecaptcha_cfg');
    }
  } catch (e) { out.hints.push('cfg walk failed: ' + e.message); }

  // --- 2. the widget iframe's k= parameter --------------------------------
  try {
    for (const f of document.querySelectorAll('iframe[src*="recaptcha"]')) {
      const m = /[?&]k=([\w-]{20,})/.exec(f.getAttribute('src') || '');
      if (m) {
        if (!out.sitekey) { out.sitekey = m[1]; out.found = true; out.hints.push('sitekey from iframe k= param'); }
        else if (out.sitekey !== m[1]) out.hints.push('iframe k= disagrees with cfg: ' + m[1]);
        break;
      }
    }
  } catch (e) { out.hints.push('iframe scan failed: ' + e.message); }

  // --- 3. legacy paths, still checked in case they come back --------------
  try {
    const w = document.querySelector('captcha-widget[data-sitekey]');
    if (w) {
      out.found = true;
      out.sitekey = out.sitekey || w.getAttribute('data-sitekey');
      const dv = w.getAttribute('data-version');
      if (dv) out.hints.push('legacy captcha-widget data-version=' + dv);
      const da = w.getAttribute('data-action');
      if (da && da !== 'null') out.action = da;
    }
    const gr = document.querySelector('[data-sitekey]');
    if (gr && !out.sitekey) {
      out.sitekey = gr.getAttribute('data-sitekey'); out.found = !!out.sitekey;
      out.hints.push('sitekey from a data-sitekey attribute');
    }
  } catch (e) { out.hints.push('legacy scan failed: ' + e.message); }

  // --- 4. context -------------------------------------------------------
  try {
    const c = document.querySelector('.g-recaptcha[id], [id*="captcha"]');
    if (c) out.containerId = c.id || null;
    out.enterprise = !!(window.grecaptcha && window.grecaptcha.enterprise);
    out.badge = !!document.querySelector('.grecaptcha-badge');
    // A bframe iframe is the interactive challenge. v3 never shows one;
    // v2-invisible does when it decides to challenge.
    out.challengeFrame = !!document.querySelector('iframe[src*="bframe"]');
    out.scripts = [...document.querySelectorAll('script[src*="recaptcha"]')]
                    .map(s => (s.getAttribute('src') || '').split('?')[0]);
    // The api.js `render` parameter is the strongest v2-vs-v3 signal there
    // is, per Google's own docs: v3 loads api.js?render=<SITE_KEY>, while
    // v2 (checkbox and invisible alike) loads api.js?render=explicit.
    out.renderParam = null;
    for (const sc of document.querySelectorAll('script[src*="recaptcha"]')) {
      const m = /[?&]render=([^&]+)/.exec(sc.getAttribute('src') || '');
      if (m) { out.renderParam = decodeURIComponent(m[1]); break; }
    }
  } catch (e) { out.hints.push('context scan failed: ' + e.message); }

  return out;
}
"""


def detect_recaptcha_in_page(evaluate, page_url: str = "") -> Optional[CaptchaChallenge]:
    """Detect a reCAPTCHA by inspecting the LIVE page, not its HTML.

    `evaluate` is a callable that runs RECAPTCHA_DISCOVERY_JS in the page and
    returns the resulting dict — i.e. `page.evaluate` under Playwright,
    `driver.execute_script` under Selenium (wrap it so the arrow function is
    invoked), or an awaited `page.evaluate` under pyppeteer.

    Version inference, and why it is deliberately conservative:
      * `size == "invisible"` PLUS an interactive bframe iframe present is
        the signature of **v2-invisible**, not v3 — v3 never renders a
        challenge frame. This is what that site looked like as of
        2026-08-24, on the same sitekey that its old markup explicitly
        labelled `data-version="v3"`.
      * `size == "invisible"` with no challenge frame is treated as v3,
        which is v3's normal appearance (badge only).
      * a `normal`/`compact` size is a v2 checkbox.
    When the two signals disagree the ambiguity is recorded in the returned
    challenge's `kind` and in the log, rather than silently guessing — pick
    the wrong one and 2captcha returns a token the site rejects.
    """
    try:
        info = evaluate(RECAPTCHA_DISCOVERY_JS)
    except Exception as e:  # noqa: BLE001 - any engine's evaluate can raise
        logger.debug("In-page reCAPTCHA discovery failed: %s", e)
        return None

    if not info or not info.get("found") or not info.get("sitekey"):
        return None

    size = info.get("size")
    render = info.get("renderParam")

    # Classification, strongest signal first.
    #
    # 1. api.js's `render` parameter. Per Google's docs, v3 loads
    #    api.js?render=<SITE_KEY> while v2 — checkbox and invisible alike —
    #    loads api.js?render=explicit. v3 has no `size` concept and never
    #    renders a challenge iframe at all.
    #      https://developers.google.com/recaptcha/docs/v3
    #      https://developers.google.com/recaptcha/docs/invisible
    #    So render=explicit rules v3 out outright, and render=<the sitekey>
    #    confirms it outright.
    # 2. `size`, which only exists for v2: invisible / normal / compact.
    # 3. presence of a bframe (interactive challenge) iframe — v2 only.
    if render and render != "explicit" and render == info.get("sitekey"):
        kind = "recaptcha_v3"
    elif render == "explicit":
        kind = "recaptcha_v2_invisible" if size == "invisible" else "recaptcha_v2"
    elif size in ("normal", "compact"):
        kind = "recaptcha_v2"
    elif info.get("challengeFrame") and size == "invisible":
        kind = "recaptcha_v2_invisible"
    else:
        kind = "recaptcha_v3"

    hints = info.get("hints") or []
    logger.info("reCAPTCHA found at runtime: kind=%s sitekey=%s size=%s render=%s "
                "challengeFrame=%s container=%s hints=%s",
                kind, info["sitekey"], size, render, info.get("challengeFrame"),
                info.get("containerId"), "; ".join(hints))
    if info.get("enterprise"):
        logger.warning("This is a reCAPTCHA ENTERPRISE widget — 2captcha needs its "
                       "enterprise method, which this project does not implement.")

    return CaptchaChallenge(
        kind=kind,
        sitekey=info["sitekey"],
        action=info.get("action") or "verify",
        page_url=page_url,
        source="runtime",
        size=size,
    )


def reconcile_detections(html_challenge: Optional[CaptchaChallenge],
                          runtime_challenge: Optional[CaptchaChallenge]
                          ) -> Optional[CaptchaChallenge]:
    """Pick between the two detectors when both find something.

    They can disagree on the SAME page, and on the site this was written
    against they did. A capture
    taken through the Scraping Browser on 2026-08-24 contains, simultaneously:

      * `<captcha-widget data-version="v3" data-sitekey="6Leif..."
         data-action="null">` — the site's own wrapper element, asserting v3
      * `recaptcha/api.js?render=explicit`, a client registered with
        `size: "invisible"`, and a `bframe` challenge iframe — the documented
        **v2-invisible** signature

    Both cannot be true. `data-version` is an attribute on the site's own
    component: it says what their code believes. `render=explicit` + `size` +
    the challenge frame describe the Google loader that is actually on the
    page, which is what Google enforces and therefore what 2captcha has to
    match. So the runtime reading wins, and the disagreement is logged rather
    than quietly resolved.

    Supporting detail: `data-action="null"` in that markup means there is no
    action string. v3 scores partly on the action; a real v3 integration
    passes one. An empty action fits a v2-invisible widget wearing a stale
    v3 label better than it fits working v3.

    (Also worth knowing: the same modal served to a European residential IP
    the same day had NO `<captcha-widget>` element at all — just the
    `render=explicit` loader. That site served more than one variant of this
    modal, so neither detector alone is enough.)
    """
    if runtime_challenge and not html_challenge:
        return runtime_challenge
    if html_challenge and not runtime_challenge:
        return html_challenge
    if not html_challenge and not runtime_challenge:
        return None

    if html_challenge.kind != runtime_challenge.kind:
        logger.warning(
            "Detectors disagree on this page: static markup says %s (from the "
            "site's own data-version), the live loader says %s (render/size/"
            "challenge-frame). Trusting the loader — that's what Google "
            "enforces and what 2captcha has to match.",
            html_challenge.kind, runtime_challenge.kind)
        # Keep the action if the static markup had a real one; the runtime
        # path often can't see it.
        if html_challenge.action and html_challenge.action != "verify":
            runtime_challenge.action = html_challenge.action
    return runtime_challenge


def _v2_task_for(challenge: CaptchaChallenge, min_score: float) -> dict:
    """Build the API-v2 `task` object for a challenge.

    Task type per variant, from https://2captcha.com/api-docs:
      * v3           -> RecaptchaV3TaskProxyless, with pageAction + minScore
      * v2 invisible -> RecaptchaV2TaskProxyless with isInvisible: true
      * v2 checkbox  -> RecaptchaV2TaskProxyless

    The `*Proxyless` types let 2captcha use its OWN IP pool, which is the
    right trade for reCAPTCHA: a v2/v3 token is not bound to the address that
    produced it, so the solve and the submission may leave from different
    places. The non-proxyless variants (RecaptchaV2Task) exist for the cases
    where that is not true, and they are not wired up here.
    """
    if challenge.is_v3:
        # minScore is not free-form: 0.3 / 0.7 / 0.9 are the documented values.
        score = min(V3_ALLOWED_MIN_SCORES,
                    key=lambda allowed: abs(allowed - min_score))
        if score != min_score:
            logger.info("minScore %.2f is not one of %s — using %.1f.",
                        min_score, V3_ALLOWED_MIN_SCORES, score)
        task = {
            "type": "RecaptchaV3TaskProxyless",
            "websiteURL": challenge.page_url,
            "websiteKey": challenge.sitekey,
            "minScore": score,
        }
        # v3 scores partly on the action, so send it when it's a real one.
        # "verify" is this module's placeholder for "the page didn't say".
        if challenge.action and challenge.action != "verify":
            task["pageAction"] = challenge.action
        return task

    task = {
        "type": "RecaptchaV2TaskProxyless",
        "websiteURL": challenge.page_url,
        "websiteKey": challenge.sitekey,
    }
    if challenge.is_invisible_v2:
        task["isInvisible"] = True
    return task


def _solve_with_2captcha_v2(api_key: str, challenge: CaptchaChallenge,
                             min_score: float = 0.7, poll_interval: int = 5,
                             max_wait: int = 180) -> str:
    """Solve via API v2: createTask, then poll getTaskResult."""
    task = _v2_task_for(challenge, min_score)
    logger.info("createTask: %s (sitekey=%s)", task["type"], challenge.sitekey)

    created = requests.post(TWOCAPTCHA_CREATE_TASK_URL,
                            json={"clientKey": api_key, "task": task},
                            timeout=30)
    created.raise_for_status()
    payload = created.json()
    if payload.get("errorId"):
        raise RuntimeError(
            f"createTask failed: {payload.get('errorCode')} — "
            f"{payload.get('errorDescription')}")
    task_id = payload["taskId"]

    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        got = requests.post(TWOCAPTCHA_GET_RESULT_URL,
                            json={"clientKey": api_key, "taskId": task_id},
                            timeout=30)
        got.raise_for_status()
        result = got.json()
        if result.get("errorId"):
            raise RuntimeError(
                f"getTaskResult failed: {result.get('errorCode')} — "
                f"{result.get('errorDescription')}")
        if result.get("status") == "ready":
            solution = result.get("solution") or {}
            # v2 returns the same string under both names.
            token = solution.get("gRecaptchaResponse") or solution.get("token")
            if not token:
                raise RuntimeError(f"task ready but no token in solution: {solution}")
            logger.info("2captcha solved %s in ~%ds.", challenge.kind, waited)
            return token
        # status == "processing"

    raise TimeoutError(f"2captcha did not return a token within {max_wait}s")


def get_balance(api_key: str) -> float:
    """Account balance via API v2 — handy for a preflight check."""
    r = requests.post(TWOCAPTCHA_BALANCE_URL, json={"clientKey": api_key}, timeout=30)
    r.raise_for_status()
    d = r.json()
    if d.get("errorId"):
        raise RuntimeError(f"getBalance failed: {d.get('errorCode')}")
    return float(d["balance"])


def _solve_with_2captcha_v1(api_key: str, challenge: CaptchaChallenge,
                             min_score: float = 0.7, poll_interval: int = 5,
                             max_wait: int = 120) -> str:
    """Legacy API v1: submit to in.php, poll res.php.

    Kept as a fallback because every live result recorded in this project's
    README came through this path. Prefer v2 for new work.

    The parameters differ per variant and are NOT interchangeable — send v3
    parameters for a v2-invisible widget and you pay for a token the site
    then rejects:
      * v3            -> version=v3, action=..., min_score=...
      * v2 invisible  -> invisible=1, no action, no min_score
      * v2 checkbox   -> neither
    """
    payload = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": challenge.sitekey,
        "pageurl": challenge.page_url,
        "json": 1,
    }
    if challenge.is_v3:
        payload.update({"version": "v3", "action": challenge.action, "min_score": min_score})
    elif challenge.is_invisible_v2:
        payload["invisible"] = 1

    logger.info("Submitting to 2captcha as %s (sitekey=%s)", challenge.kind, challenge.sitekey)
    submit = requests.post(TWOCAPTCHA_IN_URL, data=payload, timeout=30)
    submit.raise_for_status()
    payload = submit.json()
    if payload.get("status") != 1:
        raise RuntimeError(f"2captcha submit error: {payload.get('request')}")

    task_id = payload["request"]
    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        try:
            # The v1 result endpoint takes the key as a QUERY parameter, so a
            # connection error here would otherwise put it in the log.
            result = requests.get(TWOCAPTCHA_RES_URL, params={
                "key": api_key, "action": "get", "id": task_id, "json": 1,
            }, timeout=30).json()
        except requests.RequestException as exc:
            raise RuntimeError("2captcha polling request failed: %s"
                               % _redact(exc)) from None
        if result.get("status") == 1:
            logger.info("2captcha.com solved the reCAPTCHA v3 challenge.")
            return result["request"]
        if result.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"2captcha polling error: {result.get('request')}")

    raise TimeoutError("2captcha.com did not return a token in time")


def solve_recaptcha(challenge: CaptchaChallenge, twocaptcha_api_key: Optional[str],
                     api_version: str = "v2", min_score: float = 0.7) -> str:
    """Public entry point: solve `challenge` through 2captcha and return the token.

    There used to be a `use_antidetect` branch here, behind a CLI flag of the
    same name, that POSTed to a hardcoded local "solve-captcha" endpoint.
    It was removed before publishing: that endpoint was a placeholder for a
    product that does not exist under that name, so the flag could not work for
    anyone who set it, and the real "the browser solves it for you" path is
    `Captcha.setAutoSolve` over the Scraping Browser API. A flag that cannot
    succeed is worse than a missing feature — it reads as an option.
    """
    if not twocaptcha_api_key:
        raise RuntimeError(
            "A captcha was detected but no 2captcha API key was given. Pass "
            "--twocaptcha-key, or set TWOCAPTCHA_KEY. Over the Scraping Browser "
            "API you may not need either: Captcha.setAutoSolve can clear it "
            "inside the browser."
        )
    solver = _solve_with_2captcha_v1 if api_version == "v1" else _solve_with_2captcha_v2
    return solver(twocaptcha_api_key, challenge, min_score=min_score)




class CaptchaUnsolvable(RuntimeError):
    """2Captcha reported the challenge as unsolvable from this exit.

    Distinct from a generic failure because it calls for a DIFFERENT action:
    the documented remedy is to change the exit address, not to try again
    from the same one. The engines let their block-retry loop rotate on it
    rather than burning solves against an address the vendor has already
    refused.
    """




# 2Captcha's own words for "this will not solve from here". Kept as a set
# rather than a substring match so a new code is reported verbatim instead of
# being silently swallowed into the retry loop.
_UNSOLVABLE_CODES = frozenset((
    "ERROR_CAPTCHA_UNSOLVABLE",
    "ERROR_PROXY_CONNECT_REFUSED",
    "ERROR_PROXY_CONNECT_TIMEOUT",
    "ERROR_PROXY_READ_TIMEOUT",
    "ERROR_PROXY_BANNED",
    "ERROR_IP_BLOCKED",
))




INJECT_TOKEN_JS = """
(token) => {
  let el = document.getElementById('g-recaptcha-response');
  if (!el) {
    el = document.createElement('textarea');
    el.id = 'g-recaptcha-response';
    el.name = 'g-recaptcha-response';
    el.style.display = 'none';
    document.body.appendChild(el);
  }
  el.value = token;
  el.innerHTML = token;
  try {
    if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
      Object.values(window.___grecaptcha_cfg.clients).forEach((client) => {
        Object.values(client).forEach((prop) => {
          if (prop && typeof prop === 'object') {
            Object.values(prop).forEach((cb) => {
              if (typeof cb === 'function') { try { cb(token); } catch (e) {} }
            });
          }
        });
      });
    }
  } catch (e) { /* best effort, non-fatal */ }
  return true;
}
"""


# Kept as an alias: this function used to be reCAPTCHA-v3-only, and the three
# scrapers plus both modal diagnostics import it under the old name. The
# solver now branches on challenge.kind, so the name is a misnomer — the
# alias exists so older call sites keep working rather than to encourage it.
solve_recaptcha_v3 = solve_recaptcha




# ===========================================================================
# DataDome — the challenge Expedia's "Bot or Not?" handler actually picks here
# ===========================================================================
# Every measured refusal on this site named `whichChallenge:
# datadome-challenge`. The handler page then loads DataDome's own tag from
# the site's origin (`/dd/tags.js`, endpoint `/dd/js/`). When the tag's post
# is refused it fires `dd_blocked`, and the page mounts DataDome's slider
# iframe (`geo.captcha-delivery.com/captcha/?...`) inside
# `#DATADOME-CHALLENGE`. When the tag's post is NOT refused, the page
# validates by itself and leaves for the destination: that is the
# self-clearing case, and it costs nothing, so it is waited for first.
#
# What the site's own challenge bundle (captcha-pwa root-challenge.*.js,
# read 2026-10-23) does after the slider passes: on `dd_response_passed` it
# POSTs `validatePath` (`/botOrNot/validate`) with the header
# `X-Exchange-Handshake: <exchangeHandshake>` and the JSON body
# `{"datadome-response": {"ddChallengeType": <responseType>},
#   "destination": <destination>, "exchangeHandshake": <exchangeHandshake>}`,
# and on `{"success": true}` replaces the location with the destination. The
# tag runs with `disableAutoRefreshOnCaptchaPassed`, so the cookie alone is
# not what the page waits for. That is read from the served code, not from a
# captured exchange: `ddChallengeType` here is "captcha", the slider's type,
# and has not been seen on the wire.
#
# The 2Captcha side, per https://2captcha.com/api-docs/datadome-slider-captcha
# (checked 2026-10-23): task type `DataDomeSliderTask`, required fields
# websiteURL, captchaUrl (the iframe's src), userAgent, proxyType,
# proxyAddress, proxyPort; proxyLogin/proxyPassword optional. The answer is
# `solution.cookie`, a `datadome=...; Path=/; Secure; SameSite=Lax` string.
# A proxy is REQUIRED, and it has to be the exit the browser uses: the cookie
# is bound to the address that earned it. A captchaUrl with `t=bv` means
# the exit is banned and the docs say to change it, so nothing is bought.

DATADOME_TASK_TYPE = "DataDomeSliderTask"
# 38-68 s measured for 5 solves on 2026-09-14, so 180 s is ample headroom
# without letting a stuck task hold the run.
DATADOME_MAX_WAIT_S = 180
DATADOME_POLL_S = 5
# How long the challenge page gets to either mount the slider or clear by
# itself before anything is decided. The widget mounted about 3 s after
# load when measured.
DATADOME_SETTLE_MS = 15000
DATADOME_CONTAINER_SELECTOR = "#DATADOME-CHALLENGE"
DATADOME_FRAME_SELECTOR = 'iframe[src*="captcha-delivery.com"]'
DATADOME_COOKIE = "datadome"
# The slider's response type, for the site's own validate step.
DATADOME_RESPONSE_TYPE = "captcha"

_CAPTCHA_DELIVERY_HOST_RE = re.compile(r"(^|\.)captcha-delivery\.com$", re.I)


def captcha_url_verdict(captcha_url: Optional[str]) -> Tuple[bool, str]:
    """(worth buying?, reason) for a DataDome iframe src.

    Checked BEFORE the purchase. `t=bv` is DataDome saying the exit itself
    is banned, and 2Captcha's docs say a solve will not help from there.
    An interstitial (the device check, not the slider) is not what
    `DataDomeSliderTask` solves.
    """
    if not captcha_url:
        return False, "no_widget"
    parsed = urlparse(captcha_url)
    if parsed.scheme != "https" or not _CAPTCHA_DELIVERY_HOST_RE.search(
            parsed.hostname or ""):
        return False, "not_captcha_delivery"
    if "/interstitial" in parsed.path:
        return False, "interstitial"
    t = dict(parse_qsl(parsed.query)).get("t")
    if t == "bv":
        return False, "exit_banned"
    return True, f"t={t}" if t else "t=?"


def task_proxy_fields(proxy_url: Optional[str]) -> dict:
    """The task's proxy fields for the exit the browser uses.

    `https://` maps to `http`: 2Captcha's proxyType takes http, socks4 or
    socks5, and an HTTP proxy reached over TLS is still an HTTP proxy to it.
    Raises ValueError (credentials masked) for a URL it cannot express.
    """
    parsed = urlparse(proxy_url or "")
    scheme = (parsed.scheme or "").lower()
    kind = {"http": "http", "https": "http", "socks4": "socks4",
            "socks5": "socks5", "socks5h": "socks5"}.get(scheme)
    if not kind or not parsed.hostname or not parsed.port:
        raise ValueError("cannot pass this proxy to the solver: %s"
                         % _redact(proxy_url))
    fields = {"proxyType": kind, "proxyAddress": parsed.hostname,
              "proxyPort": int(parsed.port)}
    if parsed.username:
        fields["proxyLogin"] = unquote(parsed.username)
    if parsed.password:
        fields["proxyPassword"] = unquote(parsed.password)
    return fields


def datadome_task(website_url: str, captcha_url: str, user_agent: str,
                  proxy_url: str) -> dict:
    """The `task` object for createTask."""
    task = {"type": DATADOME_TASK_TYPE, "websiteURL": website_url,
            "captchaUrl": captcha_url, "userAgent": user_agent}
    task.update(task_proxy_fields(proxy_url))
    return task


def solve_datadome(api_key: str, *, website_url: str, captcha_url: str,
                   user_agent: str, proxy_url: str,
                   api_base: str = TWOCAPTCHA_API_V2,
                   poll_interval: float = DATADOME_POLL_S,
                   max_wait: float = DATADOME_MAX_WAIT_S,
                   sleep=time.sleep) -> dict:
    """createTask + getTaskResult for a DataDome slider; returns
    {"cookie": str, "cost": str|None, "seconds": float}.

    Raises CaptchaUnsolvable for 2Captcha's "not from this exit" codes and
    RuntimeError/TimeoutError otherwise, every message redacted. The proxy's
    credentials travel only in the request body, never in a message.
    """
    task = datadome_task(website_url, captcha_url, user_agent, proxy_url)
    logger.info("createTask: %s via %s:%s", task["type"],
                task["proxyAddress"], task["proxyPort"])
    started = time.monotonic()
    try:
        created = requests.post(f"{api_base}/createTask",
                                json={"clientKey": api_key, "task": task},
                                timeout=30)
        created.raise_for_status()
        payload = created.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError("createTask failed: %s" % _redact(exc)) from None
    if payload.get("errorId"):
        code = payload.get("errorCode") or "?"
        cls = CaptchaUnsolvable if code in _UNSOLVABLE_CODES else RuntimeError
        raise cls(f"createTask failed: {code}")
    task_id = payload.get("taskId")
    if not task_id:
        raise RuntimeError("createTask returned no taskId")

    waited = 0.0
    while waited < max_wait:
        sleep(poll_interval)
        waited += poll_interval
        try:
            got = requests.post(f"{api_base}/getTaskResult",
                                json={"clientKey": api_key, "taskId": task_id},
                                timeout=30)
            got.raise_for_status()
            result = got.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError("getTaskResult failed: %s"
                               % _redact(exc)) from None
        if result.get("errorId"):
            code = result.get("errorCode") or "?"
            cls = CaptchaUnsolvable if code in _UNSOLVABLE_CODES else RuntimeError
            raise cls(f"getTaskResult failed: {code}")
        if result.get("status") == "ready":
            cookie = (result.get("solution") or {}).get("cookie")
            if not cookie or DATADOME_COOKIE + "=" not in cookie:
                raise RuntimeError("task ready but no datadome cookie in "
                                   "the solution")
            return {"cookie": cookie, "cost": result.get("cost"),
                    "seconds": round(time.monotonic() - started, 1)}
    raise TimeoutError(f"2captcha did not return a DataDome cookie within "
                       f"{max_wait:.0f}s")


def parse_cookie_string(text: str) -> dict:
    """A `Set-Cookie`-shaped string as {name, value, domain, path, secure,
    httpOnly, sameSite, max_age, expires}; absent attributes are None."""
    parts = [p.strip() for p in (text or "").split(";") if p.strip()]
    out = {"name": None, "value": None, "domain": None, "path": None,
           "secure": None, "httpOnly": None, "sameSite": None,
           "max_age": None, "expires": None}
    if not parts or "=" not in parts[0]:
        return out
    out["name"], out["value"] = (x.strip() for x in parts[0].split("=", 1))
    for attr in parts[1:]:
        key, _, val = attr.partition("=")
        key = key.strip().lower()
        val = val.strip()
        if key == "domain":
            out["domain"] = val
        elif key == "path":
            out["path"] = val
        elif key == "secure":
            out["secure"] = True
        elif key == "httponly":
            out["httpOnly"] = True
        elif key == "samesite":
            out["sameSite"] = val.capitalize() if val else None
        elif key == "max-age":
            try:
                out["max_age"] = int(val)
            except ValueError:
                pass
        elif key == "expires":
            out["expires"] = val
    return out


def cookie_to_install(solution_cookie: str, page_cookie: Optional[dict],
                      page_url: str, now: Optional[float] = None) -> dict:
    """The cookie to set, as {name, value, domain, path, secure, httpOnly,
    sameSite, expires(epoch s or None), attrs_from}.

    The VALUE is the solver's. The attributes mirror the cookie the
    challenge page set for itself when there is one (it was Secure and
    SameSite=Lax when measured, and the solver's string once lacked both),
    then the solver's own attributes, then a host cookie with Secure/Lax.
    A cookie set with different attributes from the site's own lands BESIDE
    it rather than replacing it, and the browser then sends both.
    """
    sol = parse_cookie_string(solution_cookie)
    host = urlparse(page_url).hostname or ""
    page_cookie = page_cookie or {}
    attrs_from = "page" if page_cookie.get("domain") else (
        "solution" if sol.get("domain") else "default")
    domain = page_cookie.get("domain") or sol.get("domain") or host
    path = page_cookie.get("path") or sol.get("path") or "/"
    secure = page_cookie.get("secure")
    if secure is None:
        secure = True if sol.get("secure") is None else bool(sol["secure"])
    same_site = page_cookie.get("sameSite") or sol.get("sameSite") or "Lax"
    http_only = page_cookie.get("httpOnly")
    if http_only is None:
        http_only = bool(sol.get("httpOnly"))
    expires = None
    base = time.time() if now is None else now
    if sol.get("max_age") is not None:
        expires = base + sol["max_age"]
    elif page_cookie.get("expires") and page_cookie["expires"] > 0:
        expires = page_cookie["expires"]
    else:
        expires = base + 365 * 24 * 3600
    return {"name": sol.get("name") or DATADOME_COOKIE,
            "value": sol.get("value") or "", "domain": domain, "path": path,
            "secure": bool(secure), "httpOnly": bool(http_only),
            "sameSite": same_site, "expires": int(expires),
            "attrs_from": attrs_from}


def _config_value(html: str, key: str) -> Optional[str]:
    """One string value out of the handler's escaped page config."""
    m = re.search(r'%s\\*"\s*:\s*\\*"((?:[^"\\]|\\\\u[0-9a-fA-F]{4}|\\u[0-9a-fA-F]{4})*)\\*"'
                  % re.escape(key), html or "")
    if not m:
        return None
    value = m.group(1)
    for _ in range(3):
        if "\\u" not in value:
            break
        value = re.sub(r"\\+u([0-9a-fA-F]{4})",
                       lambda u: chr(int(u.group(1), 16)), value)
    return value


def expedia_validate_request(html: str) -> Optional[dict]:
    """The site's own validate call for a passed DataDome challenge, as
    {path, headers, body}, or None when the page config lacks a part of it.
    """
    path = _config_value(html, "validatePath")
    handshake = _config_value(html, "exchangeHandshake")
    destination = _config_value(html, "destination")
    if not (path and path.startswith("/") and handshake and destination):
        return None
    return {
        "path": path,
        "headers": {"Accept": "application/json,text/html",
                    "Content-Type": "application/json",
                    "X-Exchange-Handshake": handshake},
        "body": {"datadome-response": {"ddChallengeType": DATADOME_RESPONSE_TYPE},
                 "destination": destination, "exchangeHandshake": handshake},
    }


def is_datadome_challenge(html: str) -> bool:
    """Expedia's handler page, with DataDome as this time's pick."""
    from product_parser import challenge_vendor, is_challenge_page
    return bool(is_challenge_page(html)
                and "datadome" in (challenge_vendor(html) or ""))


# ---------------------------------------------------------------------------
# Run-level record: what was detected, bought and accepted
# ---------------------------------------------------------------------------
_STATS_ATTR = "_captcha_stats"


class CaptchaStats:
    """Per-run captcha accounting for the sidecar.

    `solves` counts purchases ATTEMPTED (a task created, each one claimed
    from the page's SolveBudget first); `solved` counts the ones that came
    back with an answer (what 2Captcha bills); `accepted` counts answers
    after which the page classified as served. Accepted is the only one that
    says the money bought anything, and even it is not proof: a no-solve
    reload was served about one time in three when measured.
    """

    def __init__(self):
        self.kind: Optional[str] = None
        self.solves = 0
        self.solved = 0
        self.accepted = 0
        self.attempts: list = []

    def note(self, record: dict) -> dict:
        self.kind = record.get("kind") or self.kind
        if record.get("created"):
            self.solves += 1
        if record.get("returned"):
            self.solved += 1
        self.attempts.append(record)
        return record

    def settle(self, state: str) -> None:
        """The state the page classified as after the latest answer."""
        for record in reversed(self.attempts):
            if record.get("returned") and record.get("accepted") is None:
                record["accepted"] = state not in ("challenge", "blocked")
                record["state_after"] = state
                if record["accepted"]:
                    self.accepted += 1
                return
            if record.get("accepted") is not None:
                return

    def sidecar(self) -> dict:
        return {"captcha_kind": self.kind, "captcha_solves": self.solves,
                "captcha_solved": self.solved,
                "captcha_accepted": self.accepted,
                "captcha_attempts": list(self.attempts)}


def stats_of(args) -> CaptchaStats:
    stats = getattr(args, _STATS_ATTR, None)
    if stats is None:
        stats = CaptchaStats()
        try:
            setattr(args, _STATS_ATTR, stats)
        except AttributeError:
            pass
    return stats


def settle(args, state: str) -> None:
    stats_of(args).settle(state)


def captcha_fields(args, extra: Optional[dict] = None) -> dict:
    """Merge the run's captcha record into the sidecar's extra."""
    extra = dict(extra or {})
    extra.update(stats_of(args).sidecar())
    return extra


# ---------------------------------------------------------------------------
# The fallback itself, driven through named operations
# ---------------------------------------------------------------------------
@dataclass
class DataDomeOps:
    """What an engine provides. Named operations, no JavaScript across this
    boundary: each engine speaks its own driver's dialect behind them.

      html() -> Optional[str]            the page's current markup
      count(selector) -> int             matching elements (0 on any error)
      frame_src() -> Optional[str]       the DataDome iframe's src
      user_agent() -> str                navigator.userAgent of THIS browser
      page_url() -> str                  the page's current URL
      get_cookie(name, url) -> Optional[dict]   {name,value,domain,path,
                                         secure,httpOnly,sameSite,expires}
      set_cookie(cookie) -> None         install it in this browser
      post_json(path, headers, body) -> (status, parsed JSON or None)
                                         an in-page fetch, same origin
      navigate(url) -> Optional[int]     load url; the HTTP status if known
      sleep(ms) -> None
    """
    html: object
    count: object
    frame_src: object
    user_agent: object
    page_url: object
    get_cookie: object
    set_cookie: object
    post_json: object
    navigate: object
    sleep: object


def _wait_for_widget(ops: DataDomeOps, settle_ms: int, poll_ms: int):
    """('widget', src) | ('cleared', None) | ('timeout', None)."""
    waited = 0
    while True:
        src = ops.frame_src()
        if src:
            return "widget", src
        # A page mid-navigation has no markup to read; that is not "cleared".
        html = ops.html()
        if (html is not None and not ops.count(DATADOME_CONTAINER_SELECTOR)
                and not is_datadome_challenge(html)):
            return "cleared", None
        if waited >= settle_ms:
            return "timeout", None
        ops.sleep(poll_ms)
        waited += poll_ms


def datadome_fallback(ops: DataDomeOps, *, api_key: Optional[str],
                      proxy_url: Optional[str], budget, nav_url: Optional[str],
                      stats: CaptchaStats, page_num: Optional[int] = None,
                      solve=solve_datadome, settle_ms: int = DATADOME_SETTLE_MS,
                      poll_ms: int = 500) -> bool:
    """Clear a DataDome pick on this page if it can be done honestly.

    True when the page changed (cleared by itself, or a solve was applied
    and the page re-loaded), so the caller re-reads and re-classifies it;
    then `stats.settle(state)` records whether an answer was accepted.

    Nothing is bought without a key, without the proxy the browser exits
    through, without an address to re-load (a page reached by a button
    press), without a slider on the page, for a `t=bv` exit, or once the
    page's SolveBudget is spent. The budget is claimed immediately before
    createTask and is never given back.
    """
    record = {"page": page_num, "kind": "datadome", "created": False,
              "returned": False, "accepted": None}

    # Free first: the handler validates by itself when DataDome's tag is
    # not refused, so a slider is only waited for, never assumed.
    found, src = _wait_for_widget(ops, settle_ms, poll_ms)
    if found == "cleared":
        logger.info("The DataDome challenge cleared by itself — nothing "
                    "bought.")
        record["outcome"] = "self_cleared"
        stats.note(record)
        return True
    worth, why = captcha_url_verdict(src)
    record["captcha_url"] = why
    if not worth:
        logger.warning("DataDome challenge with %s — not buying a solve%s.",
                       "no slider mounted within %.0fs" % (settle_ms / 1000)
                       if why == "no_widget" else why,
                       " (DataDome marks this exit as banned; a different "
                       "exit is the remedy)" if why == "exit_banned" else "")
        record["outcome"] = why
        stats.note(record)
        return False
    if not api_key:
        logger.warning("DataDome slider on page %s, and no TWOCAPTCHA_KEY — "
                       "not solving it.", page_num)
        record["outcome"] = "no_key"
        stats.note(record)
        return False
    if not proxy_url:
        logger.warning("DataDome slider on page %s, but this browser has no "
                       "proxy that can be handed to the solver. 2Captcha "
                       "solves DataDome through the caller's own proxy, and "
                       "the cookie is bound to the exit that earned it, so a "
                       "solve from any other address would be paid for and "
                       "refused. Not solving it.", page_num)
        record["outcome"] = "no_proxy"
        stats.note(record)
        return False
    if not nav_url:
        logger.warning("DataDome slider on page %s, which was reached by a "
                       "button press: there is no address to re-load after a "
                       "solve. Not solving it.", page_num)
        record["outcome"] = "no_address"
        stats.note(record)
        return False
    if not budget.spend():
        logger.warning("This page's solve allowance (%d) is used up — not "
                       "buying another.", budget.limit)
        record["outcome"] = "budget_spent"
        stats.note(record)
        return False

    page_url = ops.page_url() or nav_url
    user_agent = ops.user_agent() or ""
    record["created"] = True
    logger.warning("DataDome slider on page %s — solving it through 2Captcha "
                   "on the browser's own exit and User-Agent.", page_num)
    try:
        answer = solve(api_key, website_url=page_url, captcha_url=src,
                       user_agent=user_agent, proxy_url=proxy_url)
    except CaptchaUnsolvable as exc:
        logger.error("2Captcha could not solve the DataDome slider from this "
                     "exit (%s).", _redact(exc))
        record["outcome"], record["error"] = "unsolvable", _redact(exc)[-80:]
        stats.note(record)
        return False
    except Exception as exc:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the DataDome slider failed (%s).", _redact(exc))
        record["outcome"], record["error"] = "solve_failed", _redact(exc)[-80:]
        stats.note(record)
        return False
    record["returned"] = True
    record["seconds"] = answer.get("seconds")
    record["cost"] = answer.get("cost")

    html_before = ops.html() or ""
    own = ops.get_cookie(DATADOME_COOKIE, page_url)
    cookie = cookie_to_install(answer["cookie"], own, page_url)
    record["cookie_attrs_from"] = cookie.pop("attrs_from")
    try:
        ops.set_cookie(cookie)
        back = ops.get_cookie(DATADOME_COOKIE, page_url) or {}
        record["cookie_applied"] = back.get("value") == cookie["value"]
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not set the DataDome cookie: %s", _redact(exc))
        record["cookie_applied"] = False

    # 1. The cookie, then the page again.
    #    A handler page that comes back is given the same settle as the
    #    first: with an accepted cookie its own tag is not refused, and the
    #    page validates and leaves by itself. A slider mounting again means
    #    DataDome did not take the cookie.
    record["reload_status"] = ops.navigate(nav_url)
    html_after = ops.html() or ""
    still = is_datadome_challenge(html_after)
    if still:
        found, _ = _wait_for_widget(ops, settle_ms, poll_ms)
        record["after_reload"] = found
        html_after = ops.html() or html_after
        still = found != "cleared"
    after = ops.get_cookie(DATADOME_COOKIE, page_url) or {}
    record["cookie_kept_after_reload"] = after.get("value") == cookie["value"]
    record["still_challenged_after_reload"] = still

    # 2. Still the handler: its own validate step, as its bundle makes it,
    #    on the handshake of the page now in front of us.
    if still:
        if not record["cookie_kept_after_reload"]:
            # The re-load answered with a cookie of its own. The validate
            # step is asked about THIS session, so the answer goes back in.
            try:
                ops.set_cookie(cookie)
                record["cookie_reapplied"] = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not re-apply the DataDome cookie: %s",
                               _redact(exc))
        request = (expedia_validate_request(html_after)
                   or expedia_validate_request(html_before))
        if request:
            try:
                status, body = ops.post_json(request["path"],
                                             request["headers"],
                                             request["body"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("The site's validate step failed: %s",
                               _redact(exc))
                status, body = None, None
            record["validate_status"] = status
            record["validate_success"] = bool(
                isinstance(body, dict) and body.get("success") is True)
            logger.info("Validate step: HTTP %s, success=%s.", status,
                        record["validate_success"])
            if record["validate_success"]:
                record["validate_reload_status"] = ops.navigate(nav_url)
        else:
            record["validate_status"] = None
            record["validate_success"] = None
    record["outcome"] = "solved"
    stats.note(record)
    return True
