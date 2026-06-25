"""
databroker.stages.scout -- turn a Candidate into a verified BrokerRecord.

Recon-first (cheap, runs here): resolve email-only brokers and fingerprint the
site with no browser. When a form recipe is needed, hand off to the browser
deep-scout (lazy import of browser-use so this module imports without it). If the
browser stack isn't present, the candidate is recorded with status=needs_human
and the recon URL, so nothing is lost.

The full browser navigation + Sonnet synthesis lives in the standalone
broker_scout module you already have; `_deep_scout` is the seam where it plugs in.
"""
from __future__ import annotations
import asyncio

from ..core import recon
from ..core.models import BrokerRecord, Candidate, Status, Method, today
from ..core.store import BrokerStore, CandidateStore
from ..core.queue import Queue
from ..core.config import CONFIG


def _record_from_recon(cand: Candidate, data: dict) -> BrokerRecord | None:
    rec = data["recon"]
    sig = data["signals"]
    base = {
        "name": cand.name or cand.domain, "domain": cand.domain,
        "opt_out_url": cand.opt_out_url or rec.opt_out_url,
        "registries": cand.registries, "signals": sig,
        "sensitivity": cand.signals.get("sensitivity", 4) if cand.signals else 4,
        "last_checked": today(),
    }
    if rec.status == recon.STATUS_BLOCKED:
        return BrokerRecord.from_dict({**base, "status": Status.BLOCKED, "scouted": True,
                                       "notes": f"recon blocked: {rec.reason}"})
    if rec.method == "email" and rec.short_circuit:
        return BrokerRecord.from_dict({**base, "method": Method.EMAIL,
            "opt_out_direct_url": f"mailto:{rec.opt_out_email}",
            "status": Status.VERIFIED, "scouted": True, "difficulty": "low",
            "last_verified": today(),
            "notes": recon.notes_summary(sig, []) + f" | email opt-out {rec.opt_out_email}"})
    return None  # needs the browser


def _deep_scout(url: str, name: str, cfg) -> dict | None:
    """Browser navigation + synthesis. Lazy import keeps browser-use optional.
    Returns findings dict or None if the browser stack is unavailable."""
    try:
        import importlib
        bs = importlib.import_module("databroker.stages.browser_scout")
    except Exception:
        return None
    return bs.scout_url(url, name, cfg)  # adapter entry point in the legacy module


async def scout_candidate(cand: Candidate, brokers: BrokerStore,
                          cfg=CONFIG, do_shots: bool = True) -> BrokerRecord:
    url = cand.opt_out_url or f"https://{cand.domain}"
    data = await asyncio.to_thread(recon.recon_and_fingerprint, url)

    rec = _record_from_recon(cand, data)
    if rec is not None:
        if do_shots and rec.method == Method.EMAIL:
            shot = await recon.capture_screenshot(url, str(cfg.screenshots_dir / f"{cand.domain}.png"))
            if shot:
                rec.screenshot = shot
        return rec

    # browser path
    seed = data["recon"].opt_out_url or url
    findings = await asyncio.to_thread(_deep_scout, seed, cand.name or cand.domain, cfg)
    sig = data["signals"]
    if findings is None:
        # degraded: no browser here. Keep recon URL, flag for the browser worker.
        return BrokerRecord.from_dict({
            "name": cand.name or cand.domain, "domain": cand.domain,
            "opt_out_url": url, "opt_out_direct_url": seed, "signals": sig,
            "registries": cand.registries, "status": Status.NEEDS_HUMAN,
            "scouted": False, "last_checked": today(),
            "notes": "recon found opt-out URL; awaiting browser deep-scout. " + recon.notes_summary(sig, [])})

    rec = BrokerRecord.from_dict({
        "name": cand.name or cand.domain, "domain": cand.domain,
        "opt_out_url": url, "opt_out_direct_url": findings.get("opt_out_direct_url", seed),
        "method": findings.get("method", Method.WEB_FORM),
        "click_path": findings.get("click_path", ""),
        "click_path_structured": findings.get("click_path_structured", []),
        "id_required": bool(findings.get("id_required")),
        "requires_listing_url": bool(findings.get("requires_listing_url")),
        "difficulty": findings.get("difficulty", "medium"),
        "registries": cand.registries, "signals": sig,
        "status": Status.NEEDS_HUMAN if findings.get("id_required") else Status.VERIFIED,
        "scouted": True, "last_checked": today(), "last_verified": today(),
        "notes": recon.notes_summary(sig, [])})
    if do_shots:
        shot = await recon.capture_screenshot(
            rec.opt_out_direct_url or seed, str(cfg.screenshots_dir / f"{cand.domain}.png"))
        if shot:
            rec.screenshot = shot
    return rec


async def worker(scout_q: Queue, brokers: BrokerStore, candidates: CandidateStore,
                 cfg=CONFIG, stop=None):
    """Drain the scout queue, scout each candidate, upsert the result."""
    while stop is None or not stop.is_set():
        item = await scout_q.get(timeout=3)
        if item is None:
            if stop is None:
                break
            continue
        try:
            cand = Candidate.from_dict(item)
            rec = await scout_candidate(cand, brokers, cfg)
            await brokers.upsert(rec)
            print(f"[scout] {cand.domain} -> {rec.status}/{rec.method}")
            await scout_q.ack(item)
        except Exception as e:
            print(f"[scout] error {item.get('domain')}: {e}")
            await scout_q.nack(item)
