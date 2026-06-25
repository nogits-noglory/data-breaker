"""
databroker.stages.crawler -- discover net-new brokers between registry updates.

Seed-and-expand, not blind crawl. Pivots, in precision order:
  1. registry deltas      (handled by stages.registry; cron it)
  2. crt.sh cert siblings (free; finds clone farms off a known broker domain)
  3. shared analytics/ad IDs (publicwww/SpyOnWeb; needs key -> behind interface)
  4. passive DNS / reverse-whois (SecurityTrails/DomainTools; key -> interface)

Every candidate passes the classify() gate (one cheap fetch) before it is queued
for the expensive scout. Paid sources are pluggable: implement a PivotSource and
register it; the free crt.sh source ships working.
"""
from __future__ import annotations
import asyncio
from typing import Protocol

import httpx

from ..core.domains import canonical_domain
from ..core.models import Candidate
from ..core.store import CandidateStore, BrokerStore
from ..core import recon, classify

UA = "databroker-pipeline/1.0"


class PivotSource(Protocol):
    name: str
    def expand(self, seed_domain: str, known: set) -> list[str]: ...


class CrtShSource:
    """Free certificate-transparency pivot. No key."""
    name = "crtsh"

    def expand(self, seed_domain: str, known: set) -> list[str]:
        return recon.discover_siblings_crtsh(seed_domain, known=known)


class SecurityTrailsSource:
    """Passive DNS via SecurityTrails. Stub: wire the API when a key is set."""
    name = "securitytrails"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def expand(self, seed_domain: str, known: set) -> list[str]:
        if not self.api_key:
            return []
        # TODO: GET https://api.securitytrails.com/v1/domain/{d}/subdomains etc.
        return []


def _fetch_html(url: str) -> str:
    try:
        with httpx.Client(timeout=15, follow_redirects=True,
                          headers={"User-Agent": UA}) as c:
            r = c.get(url if url.startswith("http") else f"https://{url}")
            return r.text if r.status_code < 400 else ""
    except Exception:
        return ""


async def run(store: CandidateStore, brokers: BrokerStore,
              sources: list[PivotSource] | None = None, seeds: list[str] | None = None) -> int:
    """Expand from seed brokers, classify, add likely brokers as candidates."""
    sources = sources or [CrtShSource()]
    known = brokers.all_domains() | set(store.items)
    # seeds default to the highest-signal known brokers (clone farms expand best)
    seeds = seeds or [r.domain for r in brokers.records.values()][:200]

    added = 0
    for seed in seeds:
        for src in sources:
            try:
                siblings = await asyncio.to_thread(src.expand, seed, known)
            except Exception as e:
                print(f"[crawler] {src.name} on {seed} failed: {e}")
                continue
            for dom in siblings:
                dom = canonical_domain(dom)
                if not dom or dom in known:
                    continue
                known.add(dom)
                # classifier gate: one cheap fetch before queueing the scout
                html = await asyncio.to_thread(_fetch_html, dom)
                sig = recon.fingerprint(f"https://{dom}", html=html, do_favicon=False)
                is_broker, conf, reasons = classify.classify(html, sig)
                if not is_broker:
                    continue
                cand = Candidate(domain=dom, source=f"crawler:{src.name}",
                                 found_via=f"sibling_of:{seed}", signals=sig,
                                 classified_broker=True, classify_confidence=conf)
                if await store.add(cand, known=brokers.all_domains()):
                    added += 1
                    print(f"[crawler] +{dom} (conf {conf}, {','.join(reasons[:2])})")
    print(f"[crawler] {added} new broker candidates")
    return added
