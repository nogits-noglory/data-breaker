#!/usr/bin/env python3
"""
databroker -- one entry point for the whole pipeline.

  databroker scrape   [--vt FILE --tx FILE --or FILE]   pull state registries
  databroker crawl                                       discover sibling brokers
  databroker discover                                    scrape + crawl in one sweep
  databroker scout    [--once]                           scout pending candidates
  databroker remove   --user USER_ID                     queue + run a user's removals
  databroker migrate  --in OLD.yaml --out data/brokers.yaml   migrate legacy YAML
  databroker stats                                       registry health snapshot

Durable SQLite queues by default so stages can run as separate processes
(scout in one terminal, remove in another) against the same data/ dir.
"""
from __future__ import annotations
import argparse
import asyncio
from pathlib import Path

from databroker.core.config import CONFIG
from databroker.core.store import BrokerStore
from databroker import orchestrator


def cmd_stats(_args):
    s = BrokerStore(CONFIG.brokers_yaml)
    from collections import Counter
    st = Counter(r.status for r in s.records.values())
    me = Counter(r.method for r in s.records.values())
    print(f"brokers: {len(s.records)}")
    print("status:", dict(st))
    print("method:", dict(me))
    print("actionable (auto-removable):", len(s.actionable()))
    print("due for re-scout:", len(s.due_for_rescout(CONFIG.rescout_ttl_days)))


def main():
    ap = argparse.ArgumentParser(prog="databroker")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scrape", help="pull state registries into candidates")
    p.add_argument("--vt"); p.add_argument("--tx"); p.add_argument("--or", dest="orf")

    sub.add_parser("crawl", help="discover sibling brokers via OSINT pivots")
    sub.add_parser("discover", help="scrape + crawl")

    p = sub.add_parser("scout", help="scout pending candidates")
    p.add_argument("--once", action="store_true", default=True)
    p.add_argument("--claude-code", action="store_true",
                   help="Use Claude Code as the browser navigator+synthesizer (no API key needed)")

    p = sub.add_parser("remove", help="run removals for yourself (you are the agent)")
    p.add_argument("--profile", default="data/profile.yaml", help="your info (see profile.yaml.example)")
    p.add_argument("--live", action="store_true", help="actually submit (default is a dry run)")
    p.add_argument("--no-llm", action="store_true", default=True,
                   help="deterministic recipe replay, no model (default)")

    p = sub.add_parser("markdown", help="render the YAML into BROKERS.md (Tier 0, by hand)")
    p.add_argument("--out", default="BROKERS.md")

    p = sub.add_parser("migrate", help="migrate a legacy brokers_scouted.yaml")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out", default=str(CONFIG.brokers_yaml))

    sub.add_parser("stats", help="registry health snapshot")
    args = ap.parse_args()

    if args.cmd == "scrape":
        files = {k: v for k, v in (("vt", args.vt), ("tx", args.tx), ("or", args.orf)) if v}
        from databroker.core.store import CandidateStore
        from databroker.stages import registry
        asyncio.run(registry.run(CandidateStore(CONFIG.candidates_yaml),
                                 BrokerStore(CONFIG.brokers_yaml), files=files))
    elif args.cmd == "crawl":
        from databroker.core.store import CandidateStore
        from databroker.stages import crawler
        asyncio.run(crawler.run(CandidateStore(CONFIG.candidates_yaml),
                                BrokerStore(CONFIG.brokers_yaml)))
    elif args.cmd == "discover":
        asyncio.run(orchestrator.discover())
    elif args.cmd == "scout":
        if getattr(args, "claude_code", False):
            CONFIG.claude_code = True
            CONFIG.scout_concurrency = 1  # one broker at a time so request/response files don't collide
        asyncio.run(orchestrator.run_scout_pool(once=args.once))
    elif args.cmd == "remove":
        import yaml
        from databroker.core.models import User
        from databroker.stages.drivers import PlaywrightDriver, SmtpMailDriver
        prof = yaml.safe_load(open(args.profile)) if Path(args.profile).exists() else {}
        if not prof:
            print(f"No profile at {args.profile}. Copy data/profile.yaml.example and fill it in.")
            return
        me = User(user_id=prof.get("user_id", "self"), name=prof.get("name", ""),
                  emails=prof.get("emails", []), phones=prof.get("phones", []),
                  addresses=prof.get("addresses", []), dob=prof.get("dob", ""),
                  regions=prof.get("regions", ["US"]))
        async def go():
            await orchestrator.enqueue_user_removals(me)
            await orchestrator.run_remove_pool(
                lambda uid: me,
                browser=PlaywrightDriver(dry_run=not args.live),
                mail=SmtpMailDriver(dry_run=not args.live))
        if not args.live:
            print("DRY RUN (fills forms, stops before submit). Add --live to actually submit.")
        asyncio.run(go())
    elif args.cmd == "markdown":
        from scripts.generate_markdown import generate
        generate(str(CONFIG.brokers_yaml), args.out)
    elif args.cmd == "migrate":
        from scripts.migrate_yaml import migrate
        migrate(args.inp, args.out)
    elif args.cmd == "stats":
        cmd_stats(args)


if __name__ == "__main__":
    main()
