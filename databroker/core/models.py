"""
databroker.core.models -- the shared data model for the whole pipeline.

Every stage speaks these types. The YAML on disk is just a serialized list of
BrokerRecord. Dataclasses (no pydantic dep) keep this importable anywhere,
including a Raspberry Pi worker.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, asdict, fields
from typing import Optional


# enums (str constants so they serialize cleanly to YAML/JSON)
class Status:
    UNSCOUTED = "unscouted"
    VERIFIED = "verified"        # opt-out flow confirmed working
    BLOCKED = "blocked"          # anti-bot wall; retry with residential proxy
    DEAD = "dead"                # gone (404/DNS/refused)
    NEEDS_HUMAN = "needs_human"  # id required / no opt-out found
    STALE = "stale"              # was verified, past TTL, needs re-scout


class Method:
    WEB_FORM = "web_form"
    EMAIL = "email"
    PHONE = "phone"
    MAIL = "mail"
    API = "api"
    MANUAL_ONLY = "manual_only"
    UNKNOWN = "unknown"


class JobState:
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    NEEDS_HUMAN = "needs_human"   # ID upload / captcha / no auto path
    SUBMITTED = "submitted"        # request sent, awaiting confirmation
    DRY_RUN_OK = "dry_run_ok"      # recipe replayed cleanly, not submitted
    CONFIRMED = "confirmed"        # broker confirmed deletion
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"  # broker doesn't hold this user's data


def today() -> str:
    return dt.date.today().isoformat()


# broker record (one entry in brokers.yaml)
@dataclass
class BrokerRecord:
    name: str
    domain: str
    opt_out_url: str = ""
    method: str = Method.UNKNOWN
    requires_listing_url: bool = False
    confirmation: str = "unknown"
    id_required: bool = False
    difficulty: str = "unscouted"
    sensitivity: int = 4
    category: str = "Unknown"
    jurisdiction: list = field(default_factory=lambda: ["US"])
    registries: list = field(default_factory=list)
    parent_cluster: Optional[str] = None
    scouted: bool = False
    status: str = Status.UNSCOUTED
    last_checked: Optional[str] = None     # any contact attempt
    last_verified: Optional[str] = None    # flow confirmed working
    scout_tier: Optional[str] = None
    opt_out_direct_url: str = ""
    click_path: str = ""
    click_path_structured: list = field(default_factory=list)
    # applicability: how to find a person's listing on this broker (if it needs one)
    search_url_template: str = ""     # e.g. "https://x.com/search?fn={user_first}&ln={user_last}&st={user_state}"
    listing_link_pattern: str = ""    # regex capturing a listing URL from results HTML
    screenshot: str = ""
    signals: dict = field(default_factory=dict)  # OSINT fingerprint
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "BrokerRecord":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in known}
        # tolerate the old schema: derive status if absent
        if "status" not in kwargs:
            kwargs["status"] = _infer_status(d)
        return cls(**kwargs)

    def to_dict(self) -> dict:
        return asdict(self)

    def is_actionable(self) -> bool:
        """Can the removal agent attempt this automatically?"""
        return (self.status == Status.VERIFIED
                and self.method in (Method.WEB_FORM, Method.EMAIL, Method.API)
                and not self.id_required)

    def needs_rescout(self, ttl_days: int = 90) -> bool:
        if self.status in (Status.UNSCOUTED, Status.STALE):
            return True
        if not self.last_verified:
            return self.status not in (Status.DEAD,)
        age = (dt.date.today() - dt.date.fromisoformat(self.last_verified)).days
        return age > ttl_days


def _infer_status(d: dict) -> str:
    notes = str(d.get("notes", "")).upper()
    if "DEAD" in notes:
        return Status.DEAD
    if "BLOCKED" in notes or "403" in notes:
        return Status.BLOCKED
    if d.get("id_required") is True:
        return Status.NEEDS_HUMAN
    if d.get("scouted") and d.get("method") not in (None, "", "unknown"):
        return Status.VERIFIED
    return Status.UNSCOUTED


# discovery candidate (pre-scout)
@dataclass
class Candidate:
    domain: str
    name: str = ""
    opt_out_url: str = ""
    opt_out_email: str = ""
    source: str = ""              # "registry:CA", "crawler:crtsh", ...
    found_via: str = ""
    found_on: str = field(default_factory=today)
    registries: list = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    classified_broker: Optional[bool] = None
    classify_confidence: float = 0.0
    scouted: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)


# user + removal job (removal agent)
@dataclass
class User:
    user_id: str
    name: str = ""
    emails: list = field(default_factory=list)
    phones: list = field(default_factory=list)
    addresses: list = field(default_factory=list)   # [{street,city,state,zip,country}]
    dob: str = ""
    regions: list = field(default_factory=lambda: ["US"])  # privacy regimes that apply
    authorization_ref: str = ""    # pointer to signed LPOA (not the doc itself)
    idv_status: str = "unverified" # unverified | verified | failed (via IDV vendor token)

    def profile(self) -> dict:
        """Flat dict used to fill recipe template vars. No raw ID material."""
        addr = self.addresses[0] if self.addresses else {}
        parts = self.name.split()
        return {
            "user_name": self.name,
            "user_first": parts[0] if parts else "",
            "user_last": parts[-1] if len(parts) > 1 else "",
            "user_email": self.emails[0] if self.emails else "",
            "user_phone": self.phones[0] if self.phones else "",
            "user_address": addr.get("street", ""),
            "user_city": addr.get("city", ""),
            "user_state": addr.get("state", ""),
            "user_zip": addr.get("zip", ""),
        }


@dataclass
class RemovalJob:
    user_id: str
    broker_domain: str
    state: str = JobState.QUEUED
    attempts: int = 0
    last_attempt: Optional[str] = None
    confirmation_token: str = ""
    evidence_path: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "RemovalJob":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)
