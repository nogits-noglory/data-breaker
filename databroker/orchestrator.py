"""
databroker.orchestrator -- run the stages concurrently over shared queues.

Pipeline:

  registry ─┐
            ├─> CandidateStore ──> scout_q ──> [scout workers] ──> BrokerStore
  crawler  ─┘                                                          │
                                                                       v
  user signup ──> for each actionable broker ──> remove_q ──> [remove workers]
                                                                  │        │
                                                                  v        v
                                                              submitted  human_q

Discovery (registry+crawler) runs on a schedule. Scouting and removal run as
long-lived worker pools. One process here for simplicity; the SqliteQueue makes
it safe to run several of these processes against the same data dir later.
"""
from __future__ import annotations
import asyncio

from .core.config import CONFIG
from .core.queue import InMemoryQueue, SqliteQueue
from .core.store import BrokerStore, CandidateStore
from .core.models import Candidate
from .stages import registry, crawler, scout, remover


def make_queue(name: str, durable: bool, cfg=CONFIG):
    return SqliteQueue(cfg.queue_db, name) if durable else InMemoryQueue()


async def feed_scout_queue(candidates: CandidateStore, scout_q, brokers: BrokerStore):
    """Push pending, not-yet-known candidates onto the scout queue."""
    n = 0
    for cand in candidates.pending():
        if not brokers.has(cand.domain):
            await scout_q.put(cand.to_dict())
            n += 1
    print(f"[orchestrator] queued {n} candidates for scouting")
    return n


async def discover(cfg=CONFIG, files: dict | None = None):
    """One discovery sweep: registries + crawler -> candidate store."""
    brokers = BrokerStore(cfg.brokers_yaml)
    candidates = CandidateStore(cfg.candidates_yaml)
    await registry.run(candidates, brokers, files=files)
    await crawler.run(candidates, brokers)
    return candidates, brokers


async def run_scout_pool(cfg=CONFIG, durable=True, once=True):
    brokers = BrokerStore(cfg.brokers_yaml)
    candidates = CandidateStore(cfg.candidates_yaml)
    cfg.ensure_dirs()
    scout_q = make_queue("scout", durable, cfg)
    await feed_scout_queue(candidates, scout_q, brokers)
    stop = asyncio.Event() if not once else None
    workers = [asyncio.create_task(scout.worker(scout_q, brokers, candidates, cfg, stop))
               for _ in range(cfg.scout_concurrency)]
    await asyncio.gather(*workers)


async def enqueue_user_removals(user, cfg=CONFIG, durable=True, fetcher=None):
    """Run each actionable broker through the applicability gate, then queue:
    submit (with listing_url if resolved), skip if not applicable, or route to human."""
    from .core import recon
    from .stages import applicability
    brokers = BrokerStore(cfg.brokers_yaml)
    remove_q = make_queue("remove", durable, cfg)
    human_q = make_queue("human", durable, cfg)
    fetcher = fetcher or recon._default_fetcher
    counts = {"submit": 0, "skip_not_listed": 0, "needs_human": 0}
    for rec in brokers.actionable():
        d = await asyncio.to_thread(applicability.gate, rec, user, fetcher)
        counts[d.action] = counts.get(d.action, 0) + 1
        if d.action == "submit":
            await remove_q.put({"user_id": user.user_id, "broker_domain": rec.domain,
                                "listing_url": d.listing_url})
        elif d.action == "needs_human":
            await human_q.put({"user_id": user.user_id, "broker_domain": rec.domain,
                               "reason": d.reason})
    print(f"[orchestrator] {user.user_id}: submit={counts['submit']} "
          f"skip={counts['skip_not_listed']} human={counts['needs_human']}")
    return counts


async def run_remove_pool(get_user, cfg=CONFIG, durable=True, browser=None, mail=None, once=True):
    brokers = BrokerStore(cfg.brokers_yaml)
    remove_q = make_queue("remove", durable, cfg)
    human_q = make_queue("human", durable, cfg)
    rmv = remover.Remover(browser=browser, mail=mail)
    stop = None if once else asyncio.Event()
    workers = [asyncio.create_task(
                   remover.worker(remove_q, brokers, get_user, rmv, human_q, stop))
               for _ in range(cfg.remove_concurrency)]
    await asyncio.gather(*workers)
