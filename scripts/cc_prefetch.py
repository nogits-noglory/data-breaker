"""
Quick HTTP prefetch for a broker domain, gives Claude Code context to decide
the next browser action without waiting for the full browser session.
Usage: python scripts/cc_prefetch.py <domain_or_url>
"""
import sys, re
import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
OPT_PATHS = ["/opt-out", "/optout", "/do-not-sell", "/do-not-sell-my-info",
             "/ccpa", "/ccpa-opt-out", "/privacy-choices", "/your-privacy-choices",
             "/data-request", "/dsar", "/privacy-rights", "/privacy-center",
             "/privacy-policy", "/privacy"]
OPT_PAT = re.compile(r"(opt[\s\-]?out|do[\s\-]?not[\s\-]?sell|dsar|privacy.request|data.removal|remove.my.info|your.privacy.choices|ccpa|gdpr)", re.I)
EMAIL_PAT = re.compile(r"\b(privacy|optout|opt-out|dpo|datarequest|dsar|removal)@[\w.\-]+\.[a-z]{2,}\b", re.I)

def fetch(url):
    try:
        with httpx.Client(follow_redirects=True, timeout=8, headers={"User-Agent": UA}) as c:
            r = c.get(url)
            return r.status_code, str(r.url), r.text[:4000]
    except Exception as e:
        return 0, url, str(e)[:100]

def check(url):
    status, final_url, text = fetch(url)
    if status == 0:
        return None
    opt_matches = OPT_PAT.findall(text)
    emails = EMAIL_PAT.findall(text)
    has_form = "<form" in text.lower()
    blocked = status in (403, 429, 503) or "cloudflare" in text.lower()
    return {
        "url": final_url, "status": status, "blocked": blocked,
        "opt_matches": list(set(m.lower() for m in opt_matches))[:5],
        "emails": emails[:3], "has_form": has_form,
        "snippet": text[:300].replace("\n", " ")
    }

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    if not target:
        sys.exit(1)
    if not target.startswith("http"):
        target = "https://" + target

    # Try homepage first
    result = check(target)
    if result:
        print(f"HOME: {result['url']} [{result['status']}] blocked={result['blocked']}")
        print(f"  opt={result['opt_matches']} emails={result['emails']} form={result['has_form']}")

    # Try opt-out paths
    from urllib.parse import urlsplit
    base = "{0.scheme}://{0.netloc}".format(urlsplit(target))
    for path in OPT_PATHS:
        r = check(base + path)
        if r and r["status"] < 400 and r["opt_matches"]:
            print(f"HIT: {r['url']} [{r['status']}] opt={r['opt_matches']} form={r['has_form']}")
            if r["emails"]:
                print(f"  EMAIL: {r['emails']}")
            break
