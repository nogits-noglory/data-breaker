"""
databroker.stages.remover -- the autonomous removal agent.

Core idea (the unit-economics lever): the scout pays the expensive LLM once to
learn each broker's recipe (click_path_structured). Removal is then deterministic
replay -- fill the known fields at the known URL -- which is cheap and fast. The
LLM is only a fallback when replay breaks.

Flow per (user, broker):
  resolve_recipe()  substitute {user_*} placeholders from the user's profile
  triage()          decide auto vs needs_human (id upload, captcha, listing URL,
                    missing data, manual-only, no recipe)
  execute()         replay via a BrowserDriver (web_form) or send mail (email).
                    Drivers are seams: real Playwright/SMTP plug in here.

Nothing about a user's raw ID lives here; only the flat profile fields a form
needs. ID verification happened upstream via an IDV vendor token.
"""
from __future__ import annotations
import asyncio
import re
from typing import Protocol

from ..core.models import BrokerRecord, User, RemovalJob, JobState, Method, today

_VAR = re.compile(r"\{(user_[a-z_]+|listing_url|captcha_solution|user_ip)\}")


# recipe resolution
def resolve_recipe(record: BrokerRecord, profile: dict) -> tuple[list, list]:
    """Substitute template vars in the structured steps. Returns (steps, missing)."""
    missing = []

    def substitute(text: str) -> str:
        def sub(m):
            key = m.group(1)
            if key in profile and profile[key]:
                return profile[key]
            missing.append(key)
            return m.group(0) if key in ("captcha_solution", "listing_url", "user_ip") else ""
        return _VAR.sub(sub, str(text))

    resolved = []
    for step in (record.click_path_structured or []):
        if not isinstance(step, dict):
            return [], ["__corrupt_recipe__"]   # string-encoded recipe, cannot replay
        s = dict(step)
        if s.get("action") == "fill":
            s["value"] = substitute(s.get("value", ""))
        elif s.get("action") == "navigate" and s.get("url"):
            s["url"] = substitute(s["url"])      # listing URL is often the nav target
        resolved.append(s)
    return resolved, sorted(set(missing))


# triage
def triage(record: BrokerRecord, missing: list) -> str:
    """Return JobState.QUEUED (auto-runnable) or JobState.NEEDS_HUMAN."""
    if record.id_required:
        return JobState.NEEDS_HUMAN
    if "listing_url" in missing:          # recipe needs a listing we don't have
        return JobState.NEEDS_HUMAN
    if "captcha_solution" in missing:
        return JobState.NEEDS_HUMAN
    if record.method == Method.MANUAL_ONLY:
        return JobState.NEEDS_HUMAN
    if record.method == Method.WEB_FORM and not record.click_path_structured:
        return JobState.NEEDS_HUMAN
    if "__corrupt_recipe__" in missing:
        return JobState.NEEDS_HUMAN
    if record.method not in (Method.WEB_FORM, Method.EMAIL):
        return JobState.NEEDS_HUMAN
    # missing only non-blocking optional fields is fine; required name/email handled by validate
    return JobState.QUEUED


# execution drivers (seams)
class BrowserDriver(Protocol):
    async def replay(self, start_url: str, steps: list) -> dict: ...


class MailDriver(Protocol):
    async def send(self, to: str, subject: str, body: str) -> dict: ...


class NullBrowserDriver:
    """Default no-op driver: validates the plan without submitting. Swap for a
    Playwright driver in production (drive resolved steps against a real page)."""
    async def replay(self, start_url, steps):
        fills = [s for s in steps if s.get("action") == "fill"]
        return {"ok": True, "submitted": False, "dry_run": True,
                "start_url": start_url, "fields_filled": len(fills)}


class NullMailDriver:
    async def send(self, to, subject, body):
        return {"ok": True, "submitted": False, "dry_run": True, "to": to}


# executor
class Remover:
    def __init__(self, browser: BrowserDriver = None, mail: MailDriver = None):
        self.browser = browser or NullBrowserDriver()
        self.mail = mail or NullMailDriver()

    async def execute(self, record: BrokerRecord, user: User, extra: dict = None) -> RemovalJob:
        job = RemovalJob(user_id=user.user_id, broker_domain=record.domain,
                         state=JobState.IN_PROGRESS, attempts=1, last_attempt=today())
        profile = {**user.profile(), **(extra or {})}
        steps, missing = resolve_recipe(record, profile)
        state = triage(record, missing)
        # a listing-required broker with no resolved listing must go to a human
        if record.requires_listing_url and not profile.get("listing_url"):
            state = JobState.NEEDS_HUMAN
        if state == JobState.NEEDS_HUMAN:
            job.state = JobState.NEEDS_HUMAN
            job.note = f"manual: {','.join(missing) or ('listing_url' if record.requires_listing_url else record.method)}"
            return job

        try:
            if record.method == Method.EMAIL:
                to = record.opt_out_direct_url.replace("mailto:", "") or ""
                body = (f"I am requesting deletion and opt-out of my personal information "
                        f"under applicable privacy law.\nName: {user.name}\n"
                        f"Email: {user.profile().get('user_email')}")
                res = await self.mail.send(to, "Data deletion / opt-out request", body)
            else:  # web_form
                start = record.opt_out_direct_url or record.opt_out_url
                res = await self.browser.replay(start, steps)
            if not res.get("ok"):
                job.state = JobState.FAILED
                job.note = res.get("error", "replay failed")[:200]
            elif res.get("submitted"):
                job.state = JobState.SUBMITTED
                job.note = "submitted"
                if record.confirmation == "email":
                    job.note += " | awaiting email confirmation"
            else:
                job.state = JobState.DRY_RUN_OK
                job.note = "dry run ok (filled, not submitted)"
        except Exception as e:
            job.state = JobState.FAILED
            job.note = f"replay error: {e}"[:200]
        return job


# worker
async def worker(remove_q, brokers, get_user, remover: Remover = None,
                 human_q=None, stop=None):
    """Consume (user_id, broker_domain) jobs; execute or route to the human queue."""
    remover = remover or Remover()
    while stop is None or not stop.is_set():
        item = await remove_q.get(timeout=3)
        if item is None:
            if stop is None:
                break
            continue
        try:
            rec = brokers.get(item["broker_domain"])
            user = get_user(item["user_id"])
            if rec is None or user is None:
                await remove_q.ack(item)
                continue
            job = await remover.execute(rec, user, extra={"listing_url": item.get("listing_url", "")})
            if job.state == JobState.NEEDS_HUMAN and human_q is not None:
                await human_q.put(job.to_dict())
            print(f"[remove] {user.user_id} x {rec.domain} -> {job.state} ({job.note})")
            await remove_q.ack(item)
        except Exception as e:
            print(f"[remove] error: {e}")
            await remove_q.nack(item)
