"""
proxy_pool.py
--------------
A pool of proxy URLs and the rules for moving between them.

Why this exists as its own module: `--proxy` was a single static string
applied once at browser launch and never changed. That is the shape of a
demo, not of the thing proxies are bought for — the reason to hold a pool is
to spread a run across exits and to leave an exit that has started getting
challenged. The README sent readers to buy proxies without showing the
pattern; this is that pattern.

Kept engine-agnostic and side-effect free (no browser, no network) so the
rotation rules are covered by the offline suite rather than only by a live
run.

**Rotating the IP alone is not enough, and this is the part that is easy to
get wrong.** Carrying the same browser session across two exits is itself a
contradiction: cookies a bot manager issued against IP A, replayed from
IP B, are a stronger signal than either address on its own. So a caller must
build a FRESH browser context (new cookie jar, new storage) for every exit
this pool hands out — see playwright_scraper.py, which tears the browser
down and relaunches rather than swapping the proxy under a live session.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import re
import secrets
import string
from typing import List, Optional
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger("proxy_pool")

# `per-run` keeps one exit for the whole run — the safest default, since a
# single session that changes address mid-flight is more suspicious than one
# that does not. `per-page` takes a new exit for every page, which is what
# spreads volume; it costs a browser relaunch per page (see the module
# docstring for why that cost is mandatory rather than incidental).
ROTATE_MODES = ("per-run", "per-page")

# Schemes Playwright's `proxy.server` accepts. socks5 carries no credentials
# there (Chromium does not support authenticated SOCKS), so a socks5:// entry
# with a user:pass in it is rejected on load rather than silently ignored at
# request time.
_SUPPORTED_SCHEMES = ("http", "https", "socks5")


class ProxyError(ValueError):
    """A proxy list that cannot be used as given."""


def parse_proxy_line(line: str, source: str = "<arg>") -> Optional[str]:
    """Validate one proxy URL. Returns it, or None for a blank/comment line.

    Raises ProxyError with the offending line named, because a typo in a
    proxy list otherwise surfaces as a connection failure on page 1 with
    nothing pointing at the cause.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Every message below reports mask(line), never the line itself. These
    # strings hold a password, and an error message is a log: a CI run
    # printed a live proxy login and password into its own public log this
    # way, from a traceback nobody expected to carry a credential.
    shown = mask(line)
    parsed = urlparse(line)
    if parsed.scheme not in _SUPPORTED_SCHEMES:
        raise ProxyError(
            f"{source}: {shown!r} — scheme must be one of "
            f"{', '.join(_SUPPORTED_SCHEMES)} (got {parsed.scheme or 'none'}). "
            f"A bare host:port is not enough; write http://host:port.")
    if not parsed.hostname:
        raise ProxyError(f"{source}: {shown!r} — no host in that URL.")
    # The PORT is validated here and not left to the first caller that reads
    # it. `urlparse` does not parse a port until you ask for one, and then it
    # raises ValueError — so a malformed entry sailed through this function
    # and blew up much later inside to_playwright as an uncaught traceback:
    # exit 1 (crash) where it should have been exit 2 (bad usage), with no
    # message saying what was wrong with the value.
    #
    # The value that caused it is worth knowing, because it is the mistake a
    # new user makes: a line from a proxy LIST FILE
    # ("http://host:port:login:password") pasted where a proxy URL belongs.
    # The extra colons become part of the port.
    try:
        parsed.port
    except ValueError:
        raise ProxyError(
            f"{source}: {shown!r} — the port is not a number. If you copied "
            f"this from a proxy list file, that format is "
            f"scheme://host:port:login:password and this expects a URL: "
            f"http://login:password@host:port") from None
    if parsed.scheme == "socks5" and (parsed.username or parsed.password):
        raise ProxyError(
            f"{source}: {shown!r} — Chromium cannot authenticate a SOCKS5 "
            f"proxy, so credentials here would be silently dropped. Use an "
            f"http:// entry for an authenticated proxy.")
    return line


def load_proxy_file(path: str) -> List[str]:
    """Read a proxy-per-line file. Blank lines and `#` comments are skipped."""
    proxies = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            entry = parse_proxy_line(raw, source=f"{path}:{lineno}")
            if entry:
                proxies.append(entry)
    if not proxies:
        raise ProxyError(f"{path}: no proxy entries found (only blanks/comments?)")
    return proxies


def mask(url: Optional[str]) -> str:
    """A proxy URL safe to log: credentials replaced, host and port kept.

    Host and port stay visible on purpose — knowing WHICH exit a run used is
    the whole point of a rotation log, and it is not the secret.

    THIS FUNCTION MUST NEVER RAISE. It is the last thing standing between a
    password and a log, and it is called precisely when something is already
    wrong with the value. An earlier version read `parsed.port`, which
    `urlparse` computes lazily and which raises ValueError on a malformed
    authority — so the masker blew up on exactly the input that most needed
    masking, and the caller printed the raw string instead. That is how a
    live proxy login and password reached a public CI log.

    Anything it cannot take apart is redacted whole rather than echoed.

    IT TAKES A BARE URL, NOT A SENTENCE. Given "a http://u:p@h:8080 b" it
    returns "?://?" — the password is gone, which is the property that
    matters, but so is the host and port the log was written to show. Every
    caller in this repo therefore passes the URL as its own `%s` argument
    and never interpolates it into a message first. For arbitrary text that
    may contain a credential anywhere, each engine has
    `_mask_credentials()`, which is a global regex and keeps the rest of the
    string intact. `smoke_test` pins both behaviours so a future edit that
    swaps them shows up as a failing check rather than as an unreadable log.
    """
    if not url:
        return "(none)"
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "?"
        # `parsed.port` raises on a malformed authority; the raw netloc is
        # not safe to fall back to, because that is where the password is.
        try:
            port = f":{parsed.port}" if parsed.port else ""
        except ValueError:
            port = ":?"
        creds = "***:***@" if (parsed.username or parsed.password) else ""
        scheme = parsed.scheme or "?"
        return f"{scheme}://{creds}{host}{port}"
    except Exception:  # noqa: BLE001 — a masker that raises is worse than a
        # vague one. Something is already wrong with this value; say so
        # without repeating it.
        return "(unparseable proxy URL, redacted)"


def to_playwright(url: Optional[str]) -> Optional[dict]:
    """Playwright's `proxy=` dict for a proxy URL, or None.

    Credentials go in their own fields rather than in `server`. Playwright
    passes `server` down to Chromium as a command-line switch, so a
    user:pass left in there would land in the browser process's argv — where
    anything on the machine that can run `ps` can read it.
    """
    if not url:
        return None
    parsed = urlparse(url)
    port = f":{parsed.port}" if parsed.port else ""
    proxy = {"server": f"{parsed.scheme}://{parsed.hostname}{port}"}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def split_credentials(url: Optional[str]):
    """(url_without_credentials, (username, password) or None).

    For the two engines that cannot take a proxy URL whole. Chromium's
    `--proxy-server=` switch has nowhere to put a password AND lands in the
    browser process's argv, where anything that can run `ps` reads it — so
    the address goes on the command line and the credentials go through the
    driver's own channel (pyppeteer's `page.authenticate`). Selenium has no
    such channel at all, which is why it strips these and warns.
    """
    if not url:
        return None, None
    parsed = urlparse(url)
    port = f":{parsed.port}" if parsed.port else ""
    scrubbed = f"{parsed.scheme}://{parsed.hostname}{port}"
    if parsed.username or parsed.password:
        return scrubbed, (parsed.username or "", parsed.password or "")
    return scrubbed, None


class ProxyPool:
    """An ordered pool of exits, plus a cursor and a rotation policy."""

    def __init__(self, proxies: List[str], rotate: str = "per-run",
                 shuffle: bool = False, rng: Optional[random.Random] = None):
        if not proxies:
            raise ProxyError("a proxy pool needs at least one entry")
        if rotate not in ROTATE_MODES:
            raise ProxyError(f"rotate must be one of {ROTATE_MODES}, got {rotate!r}")
        # Duplicates are dropped, order preserved. A pool is a set of EXITS,
        # and repeating one does not make it two: a list of fifty identical
        # entries — which is what a copied-and-pasted proxy list often is —
        # reported "exit 2/50" on every rotation while every one of them left
        # from the same address, and the single-exit warning below never
        # fired because it counted entries. A user then believes a run is
        # spread over fifty addresses when it is burning one.
        #
        # Said out loud rather than done silently: a pool quietly smaller
        # than the file that produced it is the same kind of surprise.
        seen, unique = set(), []
        for entry in proxies:
            if entry not in seen:
                seen.add(entry)
                unique.append(entry)
        if len(unique) < len(proxies):
            logger.warning(
                "Proxy pool: %d entries collapsed to %d distinct exit(s) — "
                "%d duplicate(s) dropped. Repeating an address does not "
                "spread a run across more of them.",
                len(proxies), len(unique), len(proxies) - len(unique))
        self._proxies = unique
        if shuffle:
            # Two runs started at the same minute otherwise hammer the same
            # first exit in the list.
            (rng or random).shuffle(self._proxies)
        self.rotate = rotate
        self._index = 0
        # Counted so a run can report how many exits it actually burned.
        self.rotations = 0

    def __len__(self) -> int:
        return len(self._proxies)

    @property
    def proxies(self) -> List[str]:
        """A copy of the exits, for handing a rotated view to each worker.

        A copy rather than the list itself: a worker builds its own pool from
        this, and two threads sharing one mutable list is the bug that makes
        concurrency stop being worth it.
        """
        return list(self._proxies)

    @property
    def current(self) -> str:
        return self._proxies[self._index % len(self._proxies)]

    def advance(self, reason: str) -> str:
        """Move to the next exit and return it. Wraps around the list.

        Wrapping rather than exhausting: a pool of 3 used across 50 pages is
        a legitimate configuration, and refusing to continue would be worse
        than reusing an exit. The log line says which exit and why, so a run
        that is cycling a too-small pool is visible rather than silent.
        """
        if len(self._proxies) == 1:
            logger.warning("Asked to rotate (%s) but the pool holds one exit "
                           "(%s) — staying on it. Add more with --proxy-file.",
                           reason, mask(self.current))
            return self.current
        self._index = (self._index + 1) % len(self._proxies)
        self.rotations += 1
        logger.info("Rotated proxy (%s) -> %s [exit %d/%d, rotation #%d]",
                    reason, mask(self.current), (self._index % len(self._proxies)) + 1,
                    len(self._proxies), self.rotations)
        return self.current

    def rotates_per_page(self) -> bool:
        return self.rotate == "per-page"


# ---------------------------------------------------------------------------
# One sticky exit per run on a 2Captcha residential gateway
# ---------------------------------------------------------------------------
# A gateway login with no session segment (`<id>-zone-custom-region-US`) may
# be answered from a different residential address on every connection. A
# results walk is one browser session that turns pages with a button press,
# so an exit that changes between page 1 and page 3 replays page 1's cookies
# from a new address mid-walk. The gateway pins an exit when the login
# carries `-session-<id>`, for `-sessTime-<minutes>`. So when the login has no session, one id is
# appended for the whole run, once, before any browser exists, and every
# connection of the run (the pre-check included) uses that same login.
#
# A login that already names a session is the caller's choice and is left
# exactly as given. Rotation to another pool entry is a new browser on that
# entry's own session; the proxy under a live browser is never changed.
_GATEWAY_SUFFIXES = (".proxy.2captcha.com", ".proxy.rucaptcha.com")
_SESSION_RE = re.compile(r"-session-[A-Za-z0-9]+")
_SESSTIME_RE = re.compile(r"-sessTime-\d+")
_SESSION_CHARS = string.ascii_letters + string.digits
_SESSION_LEN = 12
DEFAULT_SESSION_MINUTES = 30
# The vendor's residential dashboard offers an IP duration up to 120 minutes
# (checked 2026-10-22); a longer value would be a pin the gateway may not keep.
MAX_SESSION_MINUTES = 120
SESSION_MINUTES_ENV = "VRBO_PROXY_SESSION_MINUTES"


def is_2captcha_gateway(url: Optional[str]) -> bool:
    """Is this proxy URL one of 2Captcha's residential gateways?"""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host.endswith(_GATEWAY_SUFFIXES)


def session_pinned(url: Optional[str]) -> bool:
    """Does this gateway URL's login carry a sticky-session segment?"""
    if not is_2captcha_gateway(url):
        return False
    try:
        return bool(_SESSION_RE.search(urlparse(url).username or ""))
    except ValueError:
        return False


def _session_minutes_arg(value) -> int:
    try:
        minutes = int(str(value).strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected whole minutes from 0 to {MAX_SESSION_MINUTES}, got "
            f"{value!r}") from None
    if not 0 <= minutes <= MAX_SESSION_MINUTES:
        raise argparse.ArgumentTypeError(
            f"expected whole minutes from 0 to {MAX_SESSION_MINUTES}, got "
            f"{minutes}")
    return minutes


def add_session_flag(parser) -> None:
    parser.add_argument(
        "--proxy-session-minutes", type=_session_minutes_arg, default=None,
        metavar="N",
        help=(f"On a 2Captcha residential gateway (a host under "
              f"{' or '.join(_GATEWAY_SUFFIXES)}) whose login names no "
              f"session, append one random session id to the login for this "
              f"run, held for N minutes, so every page of the run leaves "
              f"from one exit (default {DEFAULT_SESSION_MINUTES}; 0 = do not "
              f"pin; at most {MAX_SESSION_MINUTES}; also "
              f"{SESSION_MINUTES_ENV}). A login that already carries "
              f"-session- is used as given."))


def session_minutes(args) -> int:
    """The flag, else the environment, else the default. A bad environment
    value exits 2 rather than silently falling back."""
    value = getattr(args, "proxy_session_minutes", None)
    if value is None or value == "":
        value = os.environ.get(SESSION_MINUTES_ENV, "").strip()
        if not value:
            return DEFAULT_SESSION_MINUTES
    if isinstance(value, int):
        return value
    try:
        return _session_minutes_arg(value)
    except argparse.ArgumentTypeError as e:
        raise SystemExit(f"error: {SESSION_MINUTES_ENV}: {e}") from None


def _new_session_id(rng) -> str:
    return "".join(rng.choice(_SESSION_CHARS) for _ in range(_SESSION_LEN))


def pin_session(url: str, minutes: int, rng=None) -> str:
    """`url` with a sticky session appended to its gateway login, or `url`
    unchanged when it is not a gateway, has no login, already names a
    session, or `minutes` is 0. Only the login changes."""
    if minutes <= 0 or not is_2captcha_gateway(url):
        return url
    parts = urlparse(url)
    login = parts.username or ""
    if not login or _SESSION_RE.search(login):
        return url
    sid = _new_session_id(rng or secrets.SystemRandom())
    if _SESSTIME_RE.search(login):
        # The caller chose a duration; keep it and add only the id, in the
        # position the vendor's own generator puts it.
        new_login = _SESSTIME_RE.sub(
            lambda m: f"-session-{sid}{m.group(0)}", login, count=1)
    else:
        new_login = f"{login}-session-{sid}-sessTime-{minutes}"
    port = f":{parts.port}" if parts.port else ""
    password = parts.password
    netloc = (f"{new_login}:{password}@{parts.hostname}{port}"
              if password is not None else f"{new_login}@{parts.hostname}{port}")
    return urlunparse((parts.scheme, netloc, parts.path, parts.params,
                       parts.query, parts.fragment))


def _pin_all(proxies: List[str], minutes: int, rng=None) -> List[str]:
    """Pin every gateway entry, each on its own session id, once."""
    rng = rng or secrets.SystemRandom()
    out = [pin_session(p, minutes, rng) for p in proxies]
    appended = sum(1 for a, b in zip(proxies, out) if a != b)
    kept = sum(1 for a in proxies if session_pinned(a))
    if appended:
        logger.info(
            "Pinned %d 2Captcha gateway exit(s) to a sticky session for %d "
            "min, so each page of the run leaves from the same address "
            "(--proxy-session-minutes 0 turns this off).", appended, minutes)
    if kept:
        logger.info("%d proxy login(s) already name a session; used as "
                    "given.", kept)
    return out


def session_fields(pool, extra: Optional[dict] = None,
                   login_sent: bool = True) -> dict:
    """Merge `proxy_session_pinned` into the sidecar's extra. True when the
    exit the run ended on is a gateway login carrying a session and the
    engine actually sends that login. The id itself is never recorded."""
    extra = dict(extra or {})
    extra["proxy_session_pinned"] = bool(
        pool and login_sent and session_pinned(pool.current))
    return extra


def from_args(args) -> Optional[ProxyPool]:
    """Build a pool from --proxy-file / --proxy, or None if neither is set.

    `--proxy-file` wins when both are given, and says so: silently ignoring
    one of two conflicting options is how a run ends up on an exit the
    operator did not choose.
    """
    proxy_file = getattr(args, "proxy_file", None)
    single = getattr(args, "proxy", None)
    rotate = getattr(args, "proxy_rotate", "per-run")
    minutes = session_minutes(args)

    if proxy_file:
        if single:
            logger.warning("--proxy-file and --proxy both given; using the file "
                           "and ignoring the single --proxy.")
        proxies = load_proxy_file(proxy_file)
        logger.info("Loaded %d proxy exit(s) from %s, rotation: %s",
                    len(proxies), proxy_file, rotate)
        pool = ProxyPool(proxies, rotate=rotate,
                         shuffle=getattr(args, "proxy_shuffle", False))
        # After the pool's own de-duplication: a repeated line is still one
        # exit and must not become two by getting two session ids.
        pool._proxies = _pin_all(pool._proxies, minutes)
        return pool

    if single:
        entry = parse_proxy_line(single, source="--proxy")
        if entry is None:
            raise ProxyError("--proxy was given but is empty")
        pool = ProxyPool([entry], rotate="per-run")
        pool._proxies = _pin_all(pool._proxies, minutes)
        return pool

    return None
