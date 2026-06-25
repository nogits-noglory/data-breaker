"""
databroker.core.classify -- cheap "is this a data broker?" gate.

The crawler turns infra pivots into thousands of candidate domains, most of which
are not brokers. Running the expensive browser scout on all of them is the failure
mode. This module fetches a page once and scores it on broker fingerprints so only
likely brokers reach the scout queue. Conservative by design: better to drop a
maybe-broker than to flood the scout.
"""
from __future__ import annotations
import re

# strong signals: opt-out / people-search machinery
SIGNALS = [
    (re.compile(r"do not sell( or share)? my (personal )?info", re.I), 0.35, "do_not_sell"),
    (re.compile(r"opt[\s\-]?out", re.I), 0.2, "opt_out"),
    (re.compile(r"remove (my|your) (info|information|listing|record|profile)", re.I), 0.35, "remove_listing"),
    (re.compile(r"(reverse )?(people|phone|address|email) (search|lookup)", re.I), 0.35, "people_search"),
    (re.compile(r"background (check|report)", re.I), 0.25, "background_check"),
    (re.compile(r"public records", re.I), 0.2, "public_records"),
    (re.compile(r"data (subject|deletion) request|ccpa|cpra", re.I), 0.2, "privacy_rights"),
    (re.compile(r"we are a consumer reporting agency|fcra", re.I), 0.2, "fcra"),
    (re.compile(r"\bsuppress(ion)?\b", re.I), 0.15, "suppression"),
]
# negative signals: clearly not a broker
NEGATIVE = re.compile(
    r"(add to cart|checkout|free shipping|recipe|lyrics|watch now|stream|"
    r"login to your bank|patient portal)", re.I)


def classify(html: str, signals: dict | None = None) -> tuple[bool, float, list]:
    """Return (is_broker, confidence 0-1, matched_reasons)."""
    if not html:
        return False, 0.0, []
    score = 0.0
    reasons = []
    for pat, weight, label in SIGNALS:
        if pat.search(html):
            score += weight
            reasons.append(label)
    # a people-search box plus an opt-out path is a near-certain broker
    if "people_search" in reasons and ("opt_out" in reasons or "remove_listing" in reasons):
        score += 0.2
    if NEGATIVE.search(html):
        score -= 0.4
    # shared people-search consent vendor (from recon fingerprint) nudges up
    if signals and signals.get("consent_vendor") in ("saymine", "onetrust"):
        score += 0.05
    score = max(0.0, min(1.0, score))
    return score >= 0.5, round(score, 2), reasons
