#!/usr/bin/env python3
"""
Fix malformed compound domain entries in brokers.yaml.
Caused by opt_out_url fields containing multiple URLs separated by '; ';
the domain extractor took the whole compound string as the domain.

Run AFTER the scout queue is fully drained (no active workers).
"""
from __future__ import annotations
import sys
import yaml
from pathlib import Path

ROOT = Path(__file__).parent.parent
YAML_PATH = ROOT / "data" / "brokers.yaml"

sys.path.insert(0, str(ROOT))
from databroker.core.domains import canonical_domain


def _fix_domain(domain: str, opt_out_url: str, name: str) -> str | None:
    """Return corrected domain, or None if already OK."""
    if "; " not in domain and not (domain.count(";") > 0 and " " in domain):
        return None  # looks fine

    print(f"  MALFORMED: {name!r} domain={domain!r}")

    # Strategy 1: extract from opt_out_url
    if opt_out_url:
        # Split compound opt_out_url on '; '
        parts = [p.strip() for p in opt_out_url.replace("; ", "\n").splitlines()]
        for part in parts:
            if part.startswith("http"):
                d = canonical_domain(part)
                if d and ";" not in d and " " not in d:
                    print(f"    → fixed via opt_out_url: {d!r}")
                    return d

    # Strategy 2: take last segment of compound domain (e.g. "com; example.com" → "example.com")
    segments = [s.strip() for s in domain.split(";")]
    for seg in reversed(segments):
        if "." in seg:
            # Try to extract as a domain
            d = canonical_domain("https://" + seg.lstrip("/"))
            if d and ";" not in d and " " not in d:
                print(f"    → fixed via last-segment: {d!r}")
                return d

    print(f"    → COULD NOT FIX, leaving as-is")
    return None


def main():
    if not YAML_PATH.exists():
        print(f"ERROR: {YAML_PATH} not found")
        sys.exit(1)

    with open(YAML_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data:
        print("Empty YAML, nothing to do")
        return

    fixed = 0
    for entry in data:
        domain = entry.get("domain", "")
        opt_out_url = entry.get("opt_out_url", "")
        name = entry.get("name", "?")

        corrected = _fix_domain(domain, opt_out_url, name)
        if corrected:
            entry["domain"] = corrected
            fixed += 1

    if fixed == 0:
        print("No malformed domains found, nothing to fix.")
        return

    print(f"\nFixed {fixed} entries. Saving...")

    # Rebuild header to match BrokerStore format
    scouted = sum(1 for e in data if e.get("scouted"))
    verified = sum(1 for e in data if e.get("status") == "verified")
    header = (f"# Broker registry\n# {len(data)} total, {scouted} scouted, "
              f"{verified} verified\n\n")

    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    YAML_PATH.write_text(header + body, encoding="utf-8")
    print(f"Saved {YAML_PATH}")

    # Verify no remaining malformed domains
    remaining = [e["domain"] for e in data if "; " in e.get("domain", "")]
    if remaining:
        print(f"WARNING: {len(remaining)} still malformed: {remaining}")
    else:
        print("All domains clean.")


if __name__ == "__main__":
    main()
