"""
databroker.core.store -- the single owner of brokers.yaml and candidates.yaml.

No stage writes YAML directly. They go through the store, which holds a domain
index for O(1) dedup and does the field-merge save (ported from the scout) so
concurrent stages don't clobber each other's columns. An asyncio.Lock serializes
writes within a process; cross-process durability is the queue's job, not this.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from .domains import canonical_domain
from .models import BrokerRecord, Candidate, Status

# fields where a richer scouted value should win on merge
_MERGE_FIELDS = ["method", "difficulty", "click_path", "opt_out_direct_url",
                 "id_required", "requires_listing_url", "confirmation", "notes",
                 "scout_tier", "status", "last_verified", "last_checked",
                 "screenshot", "scouted", "signals"]
_EMPTY = ("", "none", "unknown", "browser_use", "unscouted")


def _load_yaml(path: Path):
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
    return yaml.safe_load(body) or []


class BrokerStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self.records: dict[str, BrokerRecord] = {}
        self.reload()

    def reload(self):
        self.records = {}
        for d in _load_yaml(self.path):
            dom = canonical_domain(d.get("domain") or d.get("opt_out_url") or "")
            if dom:
                self.records[dom] = BrokerRecord.from_dict({**d, "domain": dom})

    def has(self, domain: str) -> bool:
        return canonical_domain(domain) in self.records

    def get(self, domain: str) -> BrokerRecord | None:
        return self.records.get(canonical_domain(domain))

    def all_domains(self) -> set:
        return set(self.records)

    def actionable(self) -> list[BrokerRecord]:
        return [r for r in self.records.values() if r.is_actionable()]

    def due_for_rescout(self, ttl_days: int) -> list[BrokerRecord]:
        return [r for r in self.records.values() if r.needs_rescout(ttl_days)]

    async def upsert(self, rec: BrokerRecord):
        """Merge a (re)scouted record in, preferring richer values, then save."""
        async with self._lock:
            dom = canonical_domain(rec.domain)
            existing = self.records.get(dom)
            if existing is None:
                self.records[dom] = rec
            else:
                merged = existing.to_dict()
                new = rec.to_dict()
                for f in _MERGE_FIELDS:
                    v = new.get(f)
                    if v not in (None,) and str(v).lower() not in _EMPTY:
                        merged[f] = v
                # keep the longer structured path
                a = new.get("click_path_structured") or []
                b = existing.click_path_structured or []
                merged["click_path_structured"] = a if len(a) >= len(b) else b
                self.records[dom] = BrokerRecord.from_dict(merged)
            self._save_unlocked()

    def _save_unlocked(self):
        recs = list(self.records.values())
        scouted = sum(1 for r in recs if r.scouted)
        verified = sum(1 for r in recs if r.status == Status.VERIFIED)
        header = (f"# Broker registry\n# {len(recs)} total, {scouted} scouted, "
                  f"{verified} verified\n\n")
        body = yaml.safe_dump([r.to_dict() for r in recs], sort_keys=False,
                              allow_unicode=True, width=100)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(header + body, encoding="utf-8")


class CandidateStore:
    """Discovered, pre-scout domains. Crawler/registry append; scout drains."""
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self.items: dict[str, Candidate] = {}
        self.reload()

    def reload(self):
        self.items = {}
        for d in _load_yaml(self.path):
            c = Candidate.from_dict(d)
            dom = canonical_domain(c.domain)
            if dom:
                c.domain = dom
                self.items[dom] = c

    async def add(self, cand: Candidate, known: set | None = None) -> bool:
        """Add if new (and not already a known broker). Returns True if added."""
        dom = canonical_domain(cand.domain)
        if not dom or dom in self.items or (known and dom in known):
            return False
        async with self._lock:
            cand.domain = dom
            self.items[dom] = cand
            self._save_unlocked()
        return True

    def pending(self) -> list[Candidate]:
        return [c for c in self.items.values() if not c.scouted]

    def _save_unlocked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            yaml.safe_dump([c.to_dict() for c in self.items.values()],
                           sort_keys=False, allow_unicode=True), encoding="utf-8")
