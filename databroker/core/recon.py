"""
scout_recon.py -- cheap recon + fingerprint layer that runs BEFORE the
browser-use + Sonnet synthesis pass in broker_scout.

Why it exists (from the scout logs):
  - 71 brokers came back is_live=true but method=unknown: nav reached the site
    and never found the opt-out. A couple of plain HTTP GETs usually find it.
  - Haiku nav underperforms, so every nav step is expensive. Seeding the browser
    with the exact opt-out URL cuts a 15-step crawl to 2-3 steps.
  - Email-only brokers need no browser at all once we spot the mailto.

What it does:
  probe()        Fetch the homepage once, scan footer/nav links and a short list
                 of well-known paths (/opt-out, /ccpa, /do-not-sell, ...). Returns
                 the best opt-out URL + method, and whether the caller can skip
                 the browser entirely (email-only case).
  fingerprint()  Pull infra signals from the same HTML (analytics/ad IDs, favicon
                 hash, form-action hosts, consent-platform vendor). Used to (a)
                 auto-confirm cluster membership without a browser and (b) find
                 sibling broker domains.
  cluster_match()Compare a site's signals to a cluster representative's.
  discover_siblings_crtsh()  Free cert-transparency pivot -> candidate new brokers.
  capture_screenshot()       Playwright shot of the opt-out page (best-effort).

All network funcs take an injectable fetcher so the logic is testable offline.
Failures degrade gracefully: a function returns what it has rather than raising.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None
try:
    import mmh3
except Exception:  # pragma: no cover
    mmh3 = None
from .domains import canonical_domain as registrable

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# status vocabulary (replaces the old "dead-or-alive" bool)
STATUS_VERIFIED = "verified"      # opt-out flow confirmed working
STATUS_BLOCKED = "blocked"        # 403/503/429/CAPTCHA wall -- likely anti-bot, retry w/ residential proxy
STATUS_DEAD = "dead"             # DNS fail / connection refused / 404 -- genuinely gone
STATUS_NEEDS_HUMAN = "needs_human"  # id required, or no opt-out found
STATUS_UNSCOUTED = "unscouted"

# opt-out detection patterns
OPT_OUT_TEXT = re.compile(
    r"(opt[\s\-]?out|do[\s\-]?not[\s\-]?sell|your privacy choices|privacy choices|"
    r"remove (my|your)|data (subject )?request|right to delete|delete my|"
    r"ccpa|cpra|gdpr|exercise (your )?(privacy )?rights|personal information request)",
    re.I)
WELL_KNOWN_PATHS = [
    "/opt-out", "/optout", "/do-not-sell", "/do-not-sell-my-info",
    "/ccpa", "/ccpa-opt-out", "/your-privacy-choices", "/privacy-choices",
    "/data-request", "/data-removal", "/remove", "/dsar",
    "/privacy", "/privacy-policy", "/privacy-rights", "/privacy-center",
]
PRIVACY_EMAIL = re.compile(
    r"\b(privacy|optout|opt-out|dpo|compliance|datarequest|dsar|removal|remove|legal)"
    r"@[a-z0-9.\-]+\.[a-z]{2,}\b", re.I)

# fingerprint patterns
PAT_GA = re.compile(r"\bUA-\d{4,10}-\d{1,4}\b")
PAT_GA4 = re.compile(r"\bG-[A-Z0-9]{8,12}\b")
PAT_GTM = re.compile(r"\bGTM-[A-Z0-9]{5,9}\b")
PAT_ADSENSE = re.compile(r"\bca-pub-\d{10,20}\b")
PAT_FBPIXEL = re.compile(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{6,20})['\"]")
CONSENT_VENDORS = {
    "onetrust": ("cdn.cookielaw.org", "otsdkstub", "onetrust"),
    "trustarc": ("consent.trustarc.com", "trustarc"),
    "saymine": ("privacy.saymine.io", "saymine"),
    "securiti": ("securiti.ai", "privaci.io"),
    "osano": ("osano.com",),
    "termly": ("termly.io",),
    "usercentrics": ("usercentrics",),
    "ketch": ("ketch.com", "ketchcdn"),
}


# small HTML parser (stdlib, no bs4 dependency)
class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []      # (text, href)
        self.forms = []        # action urls
        self.icons = []        # favicon hrefs
        self._cur_href = None
        self._cur_text = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self._cur_href = a["href"]
            self._cur_text = []
        elif tag == "form" and a.get("action"):
            self.forms.append(a["action"])
        elif tag == "link":
            rel = (a.get("rel") or "").lower()
            if "icon" in rel and a.get("href"):
                self.icons.append(a["href"])

    def handle_data(self, data):
        if self._cur_href is not None:
            self._cur_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._cur_href is not None:
            self.anchors.append((" ".join(self._cur_text).strip(), self._cur_href))
            self._cur_href = None
            self._cur_text = []


# helpers
def classify_failure(reason: str) -> str:
    """Map a preflight failure reason to blocked (anti-bot) vs dead (gone)."""
    r = (reason or "").lower()
    if any(t in r for t in ("403", "429", "503", "captcha", "cloudflare",
                            "forbidden", "rate", "access denied")):
        return STATUS_BLOCKED
    return STATUS_DEAD  # 404, connection refused, dns, timeout


def _default_fetcher(url, want_bytes=False):
    """Return dict(status, url, text, headers, content). Network; injectable."""
    if httpx is None:
        return {"status": 0, "url": url, "text": "", "headers": {}, "content": b""}
    try:
        with httpx.Client(follow_redirects=True, timeout=15,
                          headers={"User-Agent": UA}) as c:
            r = c.get(url)
            return {"status": r.status_code, "url": str(r.url),
                    "text": "" if want_bytes else r.text,
                    "headers": dict(r.headers),
                    "content": r.content if want_bytes else b""}
    except Exception as e:
        return {"status": 0, "url": url, "text": "", "headers": {},
                "content": b"", "error": str(e)[:120]}


# recon result
@dataclass
class Recon:
    status: str = STATUS_UNSCOUTED
    opt_out_url: str = ""
    method: str = "unknown"        # web_form | email | unknown
    opt_out_email: str = ""
    confidence: float = 0.0
    short_circuit: bool = False    # True -> skip the browser entirely
    candidates: list = field(default_factory=list)
    reason: str = ""


def _score_candidate(text, href, html_lower):
    score = 0.0
    blob = f"{text} {href}".lower()
    if OPT_OUT_TEXT.search(blob):
        score += 0.5
    if "<form" in html_lower:
        score += 0.3
    for vendor_marks in CONSENT_VENDORS.values():
        if any(m in href.lower() for m in vendor_marks):
            score += 0.2
            break
    return score


def probe(url: str, fetcher=None, max_path_gets: int = 6) -> Recon:
    fetcher = fetcher or _default_fetcher
    """Find the opt-out page/email cheaply. One homepage GET + a few path GETs."""
    home = fetcher(url)
    if not home.get("status") or home["status"] >= 400:
        return Recon(status=classify_failure(home.get("error") or f"HTTP {home.get('status')}"),
                     reason=home.get("error") or f"HTTP {home.get('status')}")

    base = home["url"]
    html = home.get("text", "") or ""
    html_lower = html.lower()
    p = _LinkParser()
    try:
        p.feed(html)
    except Exception:
        pass

    candidates = []  # (score, abs_url, source)
    home_reg = registrable(base)

    # 1) footer / nav anchors that look like opt-out
    for text, href in p.anchors:
        if not href or href.startswith(("#", "javascript:")):
            continue
        if href.lower().startswith("mailto:"):
            continue
        if OPT_OUT_TEXT.search(f"{text} {href}"):
            abs_url = urljoin(base, href)
            # keep same registrable domain or a known privacy-portal host
            host_ok = registrable(abs_url) == home_reg or any(
                m in abs_url.lower() for v in CONSENT_VENDORS.values() for m in v)
            if host_ok:
                candidates.append((0.6, abs_url, "footer_link"))

    # 2) privacy mailto on the homepage
    mailto = ""
    for text, href in p.anchors:
        if href.lower().startswith("mailto:"):
            addr = href.split(":", 1)[1].split("?")[0].strip()
            if PRIVACY_EMAIL.search(addr):
                mailto = addr
                break
    if not mailto:
        m = PRIVACY_EMAIL.search(html)
        if m:
            mailto = m.group(0)

    # 3) well-known paths (bounded number of GETs)
    tried = 0
    for path in WELL_KNOWN_PATHS:
        if tried >= max_path_gets:
            break
        cand_url = urljoin(base, path)
        if any(cand_url == c[1] for c in candidates):
            continue
        r = fetcher(cand_url)
        tried += 1
        if r.get("status") and r["status"] < 400:
            body = (r.get("text") or "").lower()
            if OPT_OUT_TEXT.search(body):
                score = 0.5 + (0.3 if "<form" in body else 0.0)
                candidates.append((score, r["url"], f"path:{path}"))

    # rank
    candidates.sort(key=lambda c: c[0], reverse=True)
    cand_urls = [c[1] for c in candidates]

    # decide
    if candidates and candidates[0][0] >= 0.5:
        best = candidates[0]
        # if the best page has a form, browser records the recipe + screenshot
        return Recon(status="ok", opt_out_url=best[1], method="web_form",
                     confidence=min(1.0, best[0]), short_circuit=False,
                     candidates=cand_urls, reason=best[2])
    if mailto:
        # email-only: no browser needed to learn the method
        return Recon(status="ok", opt_out_url=base, method="email",
                     opt_out_email=mailto, confidence=0.7, short_circuit=True,
                     candidates=cand_urls, reason="privacy mailto")
    return Recon(status="ok", opt_out_url="", method="unknown",
                 confidence=0.0, short_circuit=False, candidates=cand_urls,
                 reason="no clear opt-out from recon; browser needed")


# fingerprinting
def fingerprint(url: str, html: str = None, fetcher=None,
                do_favicon: bool = True) -> dict:
    fetcher = fetcher or _default_fetcher
    """Infra signals from page HTML + favicon. Returns a plain dict (YAML/JSON safe)."""
    if html is None:
        home = fetcher(url)
        html = home.get("text", "") or ""
        base = home.get("url", url)
    else:
        base = url
    sig = {
        "analytics": sorted(set(PAT_GA.findall(html)) | set(PAT_GA4.findall(html))),
        "gtm": sorted(set(PAT_GTM.findall(html))),
        "adsense": sorted(set(PAT_ADSENSE.findall(html))),
        "fbpixel": sorted(set(PAT_FBPIXEL.findall(html))),
        "consent_vendor": "",
        "form_action_hosts": [],
        "favicon_hash": None,
    }
    low = html.lower()
    for vendor, marks in CONSENT_VENDORS.items():
        if any(m in low for m in marks):
            sig["consent_vendor"] = vendor
            break
    p = _LinkParser()
    try:
        p.feed(html)
    except Exception:
        pass
    hosts = set()
    for action in p.forms:
        h = registrable(urljoin(base, action))
        if h:
            hosts.add(h)
    sig["form_action_hosts"] = sorted(hosts)

    if do_favicon and mmh3 is not None:
        try:
            icon_url = urljoin(base, p.icons[0]) if p.icons else urljoin(base, "/favicon.ico")
            r = fetcher(icon_url, want_bytes=True)
            content = r.get("content") or b""
            if content:
                # Shodan-compatible favicon hash
                sig["favicon_hash"] = mmh3.hash(base64.encodebytes(content))
        except Exception:
            pass
    return sig


def _fetcher_params(fn):
    try:
        import inspect
        return set(inspect.signature(fn).parameters)
    except Exception:
        return set()


def cluster_match(sig: dict, rep_sig: dict) -> tuple:
    """Return (score 0-1, matched_on[]) comparing a site to a cluster rep."""
    if not sig or not rep_sig:
        return 0.0, []
    matched = []
    score = 0.0
    if sig.get("favicon_hash") and sig["favicon_hash"] == rep_sig.get("favicon_hash"):
        score += 0.6
        matched.append("favicon")
    for key in ("analytics", "adsense", "fbpixel", "gtm"):
        shared = set(sig.get(key) or []) & set(rep_sig.get(key) or [])
        if shared:
            score += 0.4
            matched.append(f"{key}:{','.join(sorted(shared))}")
    shared_hosts = set(sig.get("form_action_hosts") or []) & set(rep_sig.get("form_action_hosts") or [])
    if shared_hosts:
        score += 0.4
        matched.append(f"form_host:{','.join(sorted(shared_hosts))}")
    if sig.get("consent_vendor") and sig["consent_vendor"] == rep_sig.get("consent_vendor"):
        score += 0.15
        matched.append(f"vendor:{sig['consent_vendor']}")
    return min(1.0, score), matched


# sibling discovery (free cert-transparency pivot)
def discover_siblings_crtsh(domain: str, fetcher=None,
                            known: set = None) -> list:
    fetcher = fetcher or _default_fetcher
    """Query crt.sh for cert SANs sharing the registrable domain -> sibling hosts.
    Free, no key. Returns NEW registrable domains not in `known`."""
    known = known or set()
    reg = registrable(domain)
    if not reg:
        return []
    url = f"https://crt.sh/?q=%25.{reg}&output=json"
    r = fetcher(url)
    if not r.get("status") or r["status"] >= 400 or not r.get("text"):
        return []
    import json as _json
    try:
        rows = _json.loads(r["text"])
    except Exception:
        return []
    found = set()
    for row in rows:
        for field_name in ("common_name", "name_value"):
            val = row.get(field_name, "") or ""
            for host in re.split(r"[\s,]+", val):
                host = host.strip().lstrip("*.")
                rd = registrable(host)
                if rd and rd != reg and rd not in known:
                    found.add(rd)
    return sorted(found)


# screenshot (best-effort, Playwright)
async def capture_screenshot(url: str, out_path: str, full_page: bool = True) -> str:
    """Navigate to url and save a screenshot. Returns the path, or '' on failure.
    Uses Playwright (already present via browser-use). Safe to call standalone."""
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return ""
    try:
        from pathlib import Path
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 1600})
            page = await ctx.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.screenshot(path=out_path, full_page=full_page)
            await browser.close()
        return out_path
    except Exception:
        return ""


# one-call convenience + notes summary
def recon_and_fingerprint(url: str, fetcher=None,
                          do_osint: bool = False, known: set = None) -> dict:
    fetcher = fetcher or _default_fetcher
    """Single entry point for the scout. Returns dict with recon, signals, siblings."""
    rec = probe(url, fetcher=fetcher)
    # reuse the homepage fetch result for fingerprinting where possible
    sig = fingerprint(url, fetcher=fetcher)
    siblings = []
    if do_osint and rec.status not in (STATUS_DEAD,):
        siblings = discover_siblings_crtsh(url, fetcher=fetcher, known=known or set())
    return {"recon": rec, "signals": sig, "siblings": siblings}


def notes_summary(sig: dict, siblings: list, match=None) -> str:
    bits = []
    ids = (sig.get("analytics") or []) + (sig.get("adsense") or []) + (sig.get("fbpixel") or [])
    if ids:
        bits.append("ids=" + ",".join(ids[:3]))
    if sig.get("favicon_hash") is not None:
        bits.append(f"favicon={sig['favicon_hash']}")
    if sig.get("consent_vendor"):
        bits.append(f"vendor={sig['consent_vendor']}")
    if sig.get("form_action_hosts"):
        bits.append("form_hosts=" + ",".join(sig["form_action_hosts"][:2]))
    if match and match[0] >= 0.6:
        bits.append(f"cluster_match={match[0]:.2f}({';'.join(match[1][:2])})")
    if siblings:
        bits.append(f"siblings={len(siblings)}:" + ",".join(siblings[:3]))
    return "recon: " + "; ".join(bits) if bits else ""
