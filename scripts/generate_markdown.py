#!/usr/bin/env python3
"""
scripts/generate_markdown.py -- render brokers.yaml into a human-readable
opt-out directory (BROKERS.md). This is Tier 0: anyone can read it and opt out
by hand, no code required. Regenerate whenever the YAML changes.
"""
from __future__ import annotations
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from databroker.core.store import BrokerStore
from databroker.core.models import Status, Method

CAT_ORDER = ["People Search Site", "Marketing", "Risk Mitigation", "Recruitment", "Unknown"]
STATUS_LABEL = {
    Status.VERIFIED: "verified", Status.BLOCKED: "blocked (anti-bot, may need a real browser)",
    Status.DEAD: "dead/unreachable", Status.NEEDS_HUMAN: "needs manual handling",
    Status.STALE: "stale", Status.UNSCOUTED: "not yet scouted",
}
METHOD_LABEL = {
    Method.WEB_FORM: "web form", Method.EMAIL: "email", Method.PHONE: "phone",
    Method.MAIL: "postal mail", Method.MANUAL_ONLY: "manual only", Method.UNKNOWN: "unknown",
}


def _link(text, url):
    return f"[{text}]({url})" if url and url.startswith("http") else text


def _entry(r) -> str:
    out = []
    title = _link(r.name, r.opt_out_url or f"https://{r.domain}")
    out.append(f"#### {title}")
    out.append(f"`{r.domain}` · {r.category} · sensitivity {r.sensitivity}/10")
    # how to opt out
    method = METHOD_LABEL.get(r.method, r.method)
    if r.method == Method.EMAIL and r.opt_out_direct_url.startswith("mailto:"):
        out.append(f"- **Opt out by email:** {r.opt_out_direct_url.replace('mailto:', '')}")
    else:
        link = r.opt_out_direct_url or r.opt_out_url
        out.append(f"- **Opt out via {method}:** {_link('open the opt-out page', link)}")
    # flags
    flags = [f"difficulty: {r.difficulty}",
             f"ID required: {'yes' if r.id_required else 'no'}",
             f"find your listing first: {'yes' if r.requires_listing_url else 'no'}",
             f"email confirmation: {r.confirmation}"]
    out.append("- " + " · ".join(flags))
    if r.click_path:
        out.append(f"- **Steps:** {r.click_path}")
    checked = f", last checked {r.last_checked}" if r.last_checked else ""
    out.append(f"- *Status: {STATUS_LABEL.get(r.status, r.status)}{checked}*")
    return "\n".join(out)


def generate(brokers_yaml: str, out: str):
    store = BrokerStore(brokers_yaml)
    recs = list(store.records.values())
    by_cat = defaultdict(list)
    for r in recs:
        by_cat[r.category if r.category in CAT_ORDER else "Unknown"].append(r)

    total = len(recs)
    verified = sum(1 for r in recs if r.status == Status.VERIFIED)
    id_req = sum(1 for r in recs if r.id_required)

    lines = []
    lines.append("# Data Broker Opt-Out Directory\n")
    lines.append(f"A catalogue of **{total} data brokers** and how to remove yourself from each, "
                 "built by automated scouting and verified opt-out paths. "
                 f"{verified} have a confirmed opt-out flow.\n")
    lines.append("> You are your own agent here. Pick a broker, open its opt-out page, and follow "
                 "the steps. Brokers marked *find your listing first* require you to search your "
                 f"name on their site before requesting removal. {id_req} require a photo ID.\n")
    lines.append("**Legend:** sensitivity is a rough 1-10 estimate of how invasive the data is. "
                 "*blocked* means our scanner hit an anti-bot wall; the page usually still works in "
                 "a normal browser.\n")
    # contents
    lines.append("## Contents\n")
    for cat in CAT_ORDER:
        if by_cat[cat]:
            anchor = cat.lower().replace(" ", "-")
            lines.append(f"- [{cat}](#{anchor}) ({len(by_cat[cat])})")
    lines.append("")

    for cat in CAT_ORDER:
        group = by_cat[cat]
        if not group:
            continue
        # actionable (no ID, has a flow) first, then the rest; alpha within
        group.sort(key=lambda r: (not r.is_actionable(), r.name.lower()))
        lines.append(f"\n## {cat}\n")
        for r in group:
            lines.append(_entry(r))
            lines.append("")

    lines.append("\n---\n*Generated from `data/brokers.yaml`. "
                 "Run `python cli.py markdown` to regenerate.*\n")
    Path(out).write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}: {total} brokers across {sum(1 for c in CAT_ORDER if by_cat[c])} categories")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/brokers.yaml")
    ap.add_argument("--out", default="BROKERS.md")
    a = ap.parse_args()
    generate(a.inp, a.out)
