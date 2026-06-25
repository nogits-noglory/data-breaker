"""
databroker.stages.registry -- pull confirmed brokers from state registries.

Highest-precision discovery source: these companies registered under penalty of
law. California exposes a clean CSV; the others are session/portal downloads fed
in as files. Emits Candidate objects into the CandidateStore (deduped against
known brokers), so they flow to the scout like any other discovery.
"""
from __future__ import annotations
import csv
import io
from pathlib import Path

import httpx

from ..core.domains import canonical_domain
from ..core.models import Candidate
from ..core.store import CandidateStore, BrokerStore

CA_CSV_URL = "https://cppa.ca.gov/data_broker_registry/registry.csv"
UA = "databroker-pipeline/1.0 (privacy opt-out tooling)"

SENSITIVE = {"minors": 2, "government": 2, "citizenship": 2, "sexual orientation": 2,
             "biometric": 2, "precise geolocation": 2, "reproductive": 2}


def _find_col(header, *needles):
    low = [h.lower() for h in header]
    for i, h in enumerate(low):
        if all(n in h for n in needles):
            return i
    return None


def parse_california(text: str) -> list[Candidate]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    h = rows[0]
    iname, isite, iemail = _find_col(h, "data broker name"), _find_col(h, "primary website"), _find_col(h, "contact email")
    irights = _find_col(h, "exercise their ca consumer privacy")
    sens_cols = {idx: w for n, w in SENSITIVE.items() if (idx := _find_col(h, n)) is not None}
    out = []
    for row in rows[1:]:
        def g(i): return row[i].strip() if (i is not None and i < len(row)) else ""
        name = g(iname)
        if not name:
            continue
        domain = canonical_domain(g(isite)) or canonical_domain(g(irights))
        sens = 4 + sum(w for idx, w in sens_cols.items() if g(idx).lower() in ("yes", "true", "1"))
        out.append(Candidate(
            domain=domain, name=name,
            opt_out_url=g(irights) or g(isite), opt_out_email=g(iemail),
            source="registry:CA", found_via="CA CPPA registry",
            registries=["CA"], signals={"sensitivity": min(10, sens)}))
    return out


async def run(store: CandidateStore, brokers: BrokerStore,
              ca_url: str = CA_CSV_URL, files: dict | None = None) -> int:
    """Fetch registries, add new candidates. Returns count added."""
    cands: list[Candidate] = []
    try:
        with httpx.Client(timeout=30, headers={"User-Agent": UA}) as c:
            cands += parse_california(c.get(ca_url).text)
    except Exception as e:
        print(f"[registry] CA fetch failed: {e}")
    # VT/TX/OR come in as downloaded files (portal/session based)
    for state, path in (files or {}).items():
        try:
            raw = Path(path).read_text(encoding="utf-8", errors="replace")
            for row in csv.DictReader(io.StringIO(raw)):
                name = row.get("name") or row.get("Business Name") or ""
                site = row.get("website") or row.get("url") or ""
                if name:
                    cands.append(Candidate(domain=canonical_domain(site), name=name,
                                           opt_out_url=site, source=f"registry:{state.upper()}",
                                           registries=[state.upper()]))
        except Exception as e:
            print(f"[registry] {state} file failed: {e}")

    known = brokers.all_domains()
    added = 0
    for cand in cands:
        if cand.domain and await store.add(cand, known=known):
            added += 1
    print(f"[registry] {len(cands)} parsed, {added} new candidates")
    return added
