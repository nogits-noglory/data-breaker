#!/usr/bin/env python3
"""
scripts/migrate_yaml.py -- bring a legacy brokers_scouted.yaml to the new schema.

Fixes from the data review:
  - 21 click_path_structured stored as strings (mixed JSON / python-repr) -> real lists
  - adds status + last_checked (inferred where possible)
  - dedups by registrable domain
  - drops nothing silently; prints a change report
"""
from __future__ import annotations
import ast
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from databroker.core.domains import canonical_domain
from databroker.core.models import BrokerRecord, Status


def _fix_structured(v):
    """Return (list, was_repaired, was_dropped)."""
    if isinstance(v, list):
        return v, False, False
    if not v or not isinstance(v, str):
        return [], False, False
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(v)
            if isinstance(parsed, list):
                return parsed, True, False
        except Exception:
            continue
    return [], False, True  # unparseable -> drop, flag in notes


def migrate(inp: str, out: str):
    text = Path(inp).read_text(encoding="utf-8")
    body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
    rows = yaml.safe_load(body) or []

    repaired = dropped = deduped = 0
    by_dom: dict[str, dict] = {}

    for d in rows:
        d = dict(d)
        fixed, was_rep, was_drop = _fix_structured(d.get("click_path_structured"))
        d["click_path_structured"] = fixed
        if was_rep:
            repaired += 1
        if was_drop:
            dropped += 1
            d["notes"] = (str(d.get("notes", "")) + " | recipe was corrupt, dropped in migration").strip(" |")
        # last_checked from last_verified if present
        if d.get("last_verified") and not d.get("last_checked"):
            d["last_checked"] = d["last_verified"]

        rec = BrokerRecord.from_dict(d)
        dom = canonical_domain(rec.domain or d.get("opt_out_url", ""))
        if not dom:
            continue
        rec.domain = dom
        if dom in by_dom:
            deduped += 1
            # keep the one with a longer structured recipe
            if len(rec.click_path_structured) <= len(by_dom[dom].click_path_structured or []):
                continue
        by_dom[dom] = rec

    recs = list(by_dom.values())
    scouted = sum(1 for r in recs if r.scouted)
    verified = sum(1 for r in recs if r.status == Status.VERIFIED)
    blocked = sum(1 for r in recs if r.status == Status.BLOCKED)
    header = f"# Broker registry (migrated)\n# {len(recs)} total, {scouted} scouted, {verified} verified, {blocked} blocked\n\n"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(header + yaml.safe_dump(
        [r.to_dict() for r in recs], sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8")

    print(f"in:        {len(rows)} rows")
    print(f"out:       {len(recs)} records -> {out}")
    print(f"repaired:  {repaired} corrupt recipes parsed")
    print(f"dropped:   {dropped} unparseable recipes (flagged in notes)")
    print(f"deduped:   {deduped} duplicate domains merged")
    print(f"status:    verified={verified} blocked={blocked} other={len(recs)-verified-blocked}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default="data/brokers.yaml")
    a = ap.parse_args()
    migrate(a.inp, a.out)
