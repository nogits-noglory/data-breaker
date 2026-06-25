"""
databroker.stages.applicability -- the gate between "broker exists" and
"queue a removal for THIS user".

Two jobs:
  1. Relevance: skip brokers the user isn't subject to (jurisdiction mismatch).
     For ordinary opt-out/suppression brokers, relevance is assumed (you submit a
     blanket request); there's no public per-person record to check.
  2. Listing resolution: the ~quarter of brokers with requires_listing_url=True
     cannot accept a request until you find the user's specific listing URL. This
     searches the broker (via the scouted search_url_template) and resolves it, so
     those jobs run automatically instead of falling to a human.

Outcome per (user, broker):
  submit            -> queue it (with listing_url if one was needed + found)
  skip_not_listed   -> user isn't in/subject to this broker; nothing to do
  needs_human       -> needs a listing but we couldn't resolve it automatically
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from urllib.parse import urljoin, quote

from ..core.models import BrokerRecord, User

_VAR = re.compile(r"\{(user_[a-z_]+)\}")


@dataclass
class Decision:
    action: str          # "submit" | "skip_not_listed" | "needs_human"
    listing_url: str = ""
    reason: str = ""


def _fill_template(tmpl: str, profile: dict) -> str | None:
    """Fill {user_*} in a search URL, URL-encoding values. None if a field is missing."""
    missing = []
    def sub(m):
        v = profile.get(m.group(1), "")
        if not v:
            missing.append(m.group(1))
            return ""
        return quote(str(v))
    out = _VAR.sub(sub, tmpl)
    return None if missing else out


def find_listing(broker: BrokerRecord, profile: dict, fetcher) -> tuple[str | None, str]:
    """Return (listing_url | None, status). status in:
    found | not_listed | no_template | missing_profile_fields | search_blocked."""
    if not broker.search_url_template:
        return None, "no_template"
    url = _fill_template(broker.search_url_template, profile)
    if url is None:
        return None, "missing_profile_fields"
    r = fetcher(url)
    if not r.get("status") or r["status"] >= 400 or not r.get("text"):
        return None, "search_blocked"
    html = r["text"]
    if broker.listing_link_pattern:
        m = re.findall(broker.listing_link_pattern, html)
        cands = m
    else:
        # heuristic: result links that mention the person's last name
        last = (profile.get("user_last") or "").lower()
        cands = [h for h in re.findall(r'href=["\']([^"\']+)["\']', html)
                 if last and last in h.lower()]
    if not cands:
        return None, "not_listed"
    return urljoin(url, cands[0]), "found"


def gate(broker: BrokerRecord, user: User, fetcher) -> Decision:
    # 1) jurisdiction relevance
    if broker.jurisdiction and not (set(broker.jurisdiction) & set(user.regions)):
        return Decision("skip_not_listed", reason="jurisdiction mismatch "
                        f"({broker.jurisdiction} vs {user.regions})")
    # 2) brokers that need no listing: blanket opt-out / suppression
    if not broker.requires_listing_url:
        return Decision("submit", reason="blanket opt-out")
    # 3) listing-required: try to resolve it
    listing, status = find_listing(broker, user.profile(), fetcher)
    if status == "found":
        return Decision("submit", listing_url=listing, reason="listing resolved")
    if status == "not_listed":
        return Decision("skip_not_listed", reason="not found on broker")
    return Decision("needs_human", reason=f"listing unresolved: {status}")
