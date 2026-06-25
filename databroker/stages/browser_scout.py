"""
broker_scout.py
---------------
Visits every broker in the registry, maps the opt-out path, and writes
findings back into the YAML.

Architecture:
  Navigation  -- browser-use agent driven by Haiku. Cheap. Explores the
                 page, follows links, reads content, takes actions.
  Synthesis   -- single Anthropic API call to Sonnet. Receives the full
                 agent observation log and produces clean structured output.
                 Sonnet only touches text, never the browser.

Tiers:
  Tier 0  preflight     -- HTTP HEAD check. Dead pages flagged for free.
  Tier 1  cluster_rep   -- First state in each cluster family. Full nav+synth.
  Tier 2  cluster_verify-- Remaining states. Cheap verify against rep findings.
  Tier 3  standard      -- All non-cluster brokers. Full nav+synth.

Usage:
    set ANTHROPIC_API_KEY=sk-ant-...
    python broker_scout.py [options]

Options:
    --start N              Start index (default 0)
    --end N                End index (default all)
    --category CATEGORY    Filter by category
    --sensitivity-min N    Only brokers with sensitivity >= N
    --skip-scouted         Skip already scouted entries
    --dry-run              Show plan without running
    --no-preflight         Skip HTTP preflight check
    --no-cluster-opt       Disable cluster verification optimization

Outputs:
    brokers_scouted.yaml   Updated registry
    scout_log.jsonl        Full log per broker
"""

import asyncio
import json
import os
import re
import sys
import argparse
from datetime import date
from pathlib import Path
from collections import defaultdict

import httpx
import yaml
import anthropic
from browser_use.llm.anthropic.chat import ChatAnthropic
from browser_use import Agent, BrowserProfile

from ..core import recon  # packaged recon

# paths
REGISTRY_IN   = Path(__file__).parent / "brokers.yaml"
REGISTRY_OUT  = Path(__file__).parent / "brokers_scouted.yaml"
LOG_FILE      = Path(__file__).parent / "scout_log.jsonl"
OSINT_FILE    = Path(__file__).parent / "osint_findings.jsonl"   # fingerprints + siblings
CANDIDATES_OUT = Path(__file__).parent / "discovered_candidates.yaml"  # new sibling domains
SHOTS_DIR     = Path(__file__).parent / "screenshots"
TODAY         = date.today().isoformat()

# models
# Haiku underperformed at navigation (logs: many sites reached but opt-out never
# found, constant fallback). Default nav to Sonnet; override with --nav-model.
MODEL_NAV    = "claude-sonnet-4-20250514"    # browser navigation
MODEL_SYNTH  = "claude-sonnet-4-20250514"    # structured output synthesis


class FatalAPIError(Exception):
    """Billing/auth failure -- halt the whole run instead of marking entries done."""


def _is_fatal_api_error(msg: str) -> bool:
    m = (msg or "").lower()
    return any(t in m for t in (
        "credit balance is too low", "billing", "invalid x-api-key",
        "authentication_error", "401", "permission_error"))

# cluster families
CLUSTER_REPS = {
    "state_courtrecords_us":   "Alabamacourtrecords.us",
    "state_arrests_org":       "Alabamaarrests.org",
    "state_arrestrecords_org": "Alabamaarrestrecords.org",
    "state_peoplerecords_org": "Alabama People Records",
}

# navigation task
# Haiku drives this. Goal is observation, not structured output.
NAV_TASK = """
You are a privacy researcher mapping data broker opt-out processes.

Start at: {url}
Site name: {name}

CRITICAL: You MUST navigate all the way to the actual opt-out form or mechanism.
Do NOT stop at the homepage. Do NOT stop at a privacy policy page.
Keep clicking until you reach the form where a user would actually submit a removal request.

Step-by-step instructions:
1. Load the page and look for any of these: "Privacy", "Do Not Sell", "Opt Out",
   "Data Removal", "Remove My Info", "CCPA", "Your Privacy Choices", "Contact Us"
   These are usually in the footer, navigation menu, or a cookie banner.
2. Click the most relevant link and navigate deeper.
3. Keep navigating until you reach the actual opt-out form, email address,
   phone number, or mailing address where removal can be requested.
4. Once you reach the opt-out mechanism, record in your memory:
   - The exact URL you are on
   - Every link you clicked to get here (exact button/link text)
   - The type of opt-out mechanism (web form / email address / phone number / mailing address)
   - The form fields present if it is a web form
   - Whether CAPTCHA is present
   - Whether photo ID is required
   - Whether you had to find your listing first
   - The company name or brand shown on the page
5. If you cannot find an opt-out mechanism after thorough exploration,
   record that clearly in your memory.

DO NOT fill out or submit any forms.
DO NOT enter any personal information.
Use your memory field at every step to record what you observe.

IMPORTANT: If clicking a link opens a new tab, wait for the new tab to fully load,
then continue your exploration there. Always check the current URL after tab switches.
If you see a consent popup on the new tab, dismiss it first.
"""

# verify task (cluster members)
VERIFY_TASK = """
You are a privacy researcher verifying a data broker opt-out process.

Go to: {url}
Site name: {name}

A similar site in this network has this opt-out process:
Method: {rep_method}
Path: {rep_path}
Difficulty: {rep_difficulty}

Quickly verify:
1. Is this page live?
2. Does the opt-out process match the description above?
3. Are there any differences?
4. Is there anything unusual?

Do not submit any forms. Just observe and report.
"""

# synthesis prompt
# Sonnet receives the full agent observation log and produces structured output.
SYNTH_PROMPT = """
You are analyzing a browser session that explored a data broker opt-out page.

Broker: {name}
URL: {url}
Category: {category}

Below is the complete observation log from the browser session:

URLS VISITED:
{urls}

AGENT ACTIONS TAKEN:
{actions}

AGENT OBSERVATIONS (memory and thinking at each step):
{observations}

EXTRACTED CONTENT:
{extracted}

Based on this session data, produce the structured findings.

IMPORTANT RULES:
- If the agent reached an opt-out form or mechanism, describe it precisely.
- If the agent only reached the homepage or a privacy policy without finding the opt-out,
  use the URLs visited and page content to infer the most likely opt-out path.
- For CLICK_PATH_STRUCTURED, always produce a valid JSON array even if incomplete.
  At minimum include the navigate action to the starting URL.
- For OPT_OUT_DIRECT_URL, use the last meaningful URL visited if you cannot
  determine the exact form URL. Never leave it as unknown if URLs were visited.
- Only use unknown when you genuinely have no signal at all.

Format your response EXACTLY as follows (one item per line, no extra text):
IS_LIVE: yes / no / error
METHOD: web_form | email | phone | mail | api | manual_only | unknown
REQUIRES_LISTING_URL: true / false
ID_REQUIRED: true / false
CAPTCHA: true / false
EMAIL_CONFIRMATION: true / false
CLICK_PATH: human-readable steps from {url} to opt-out form.
  Use button/link labels exactly as they appear on the page.
  Example: "Scroll to footer > Click 'Privacy Policy' > Click 'Do Not Sell My Info' > Fill name and email form"
CLICK_PATH_STRUCTURED: JSON array of action objects representing the same path as CLICK_PATH.
  Each object has: "action" (navigate/click/fill/submit/scroll), and relevant fields per action type.
  For navigate: {{"action":"navigate","url":"https://example.com"}}
  For click: {{"action":"click","label":"Do Not Sell","selector_hint":"footer a, nav a, button"}}
  For fill: {{"action":"fill","field":"email","value":"{{user_email}}"}} -- use {{user_name}}, {{user_email}}, {{user_address}}, {{user_city}}, {{user_state}}, {{user_zip}} as placeholders
  For submit: {{"action":"submit","label":"Submit"}}
  For scroll: {{"action":"scroll","direction":"down"}}
  Example: [{{"action":"navigate","url":"https://example.com"}},{{"action":"scroll","direction":"down"}},{{"action":"click","label":"Privacy Policy","selector_hint":"footer a"}},{{"action":"click","label":"Do Not Sell","selector_hint":"a, button"}},{{"action":"fill","field":"email","value":"{{user_email}}"}},{{"action":"submit","label":"Submit"}}]
OPT_OUT_DIRECT_URL: the direct URL of the opt-out form or mechanism page, not the homepage. If same as {url} write same.
DIFFICULTY: low | medium | high | manual_only
PARENT_BRAND: company name if different from "{name}", otherwise none
NOTES: automation-relevant observations, otherwise none
"""

VERIFY_SYNTH_PROMPT = """
You are analyzing a browser session that verified a data broker opt-out page.

Broker: {name}
URL: {url}
Expected process: {rep_method} via path: {rep_path}

Browser session observations:
{observations}

Answer EXACTLY:
CONFIRMED: yes / no / partial
DEAD: yes / no
DIFFERENCES: describe differences from expected, or none
NOTES: anything unusual, or none
"""


URL_REFRESH_TASK = """
You are finding the direct URL of a data broker opt-out form.

Go to: {url}
Site: {name}
Known path to opt-out: {click_path}

Follow the path above and navigate to the opt-out form.
Do NOT fill out or submit any forms.
Just navigate there and stop.
Your only goal is to land on the opt-out page and report its URL.
"""

URL_REFRESH_SYNTH = """
A browser session navigated to a data broker opt-out page.
Starting URL: {url}
Known click path: {click_path}

URLs visited during session:
{urls}

What is the direct URL of the opt-out form or mechanism page?
If the opt-out is on the homepage itself, write: same
If you cannot determine it, write: unknown

Respond with ONLY one line:
OPT_OUT_DIRECT_URL: <url>
"""

# I/O
def load_registry(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        content = f.read()
    lines = [l for l in content.split("\n") if not l.startswith("#")]
    data = yaml.safe_load("\n".join(lines))
    return data if data else []


def save_registry(entries: list[dict], path: Path) -> None:
    """
    Merge-save: read current on-disk file, update only changed entries,
    write back. Preserves manual edits made outside the script and ensures
    structured paths and direct URLs from previous runs are never lost.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Load current on-disk version if it exists
    disk_map = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                disk_content = f.read()
            disk_lines = [l for l in disk_content.split("\n") if not l.startswith("#")]
            disk_data = yaml.safe_load("\n".join(disk_lines)) or []
            disk_map = {e.get("name"): e for e in disk_data}
        except Exception:
            disk_map = {}

    merged = []
    mem_map = {e.get("name"): e for e in entries}

    for mem in entries:
        name = mem.get("name")
        disk = disk_map.get(name, {})

        if not mem.get("scouted"):
            # Not scouted -- just use memory version
            merged.append(mem)
            continue

        if not disk.get("scouted"):
            # Newly scouted this run -- use memory version
            merged.append(mem)
            continue

        # Both scouted -- merge per field, preferring richer data
        merged_entry = dict(disk)
        for field in ["method", "difficulty", "click_path", "opt_out_direct_url",
                      "id_required", "requires_listing_url", "confirmation",
                      "notes", "scout_tier", "last_verified", "last_checked",
                      "status", "screenshot", "scouted"]:
            mem_val = mem.get(field)
            if mem_val is not None and str(mem_val).lower() not in (
                    "", "none", "unknown", "browser_use"):
                merged_entry[field] = mem_val

        # Prefer longer structured path
        mem_cps = mem.get("click_path_structured") or []
        disk_cps = disk.get("click_path_structured") or []
        if isinstance(mem_cps, list) and isinstance(disk_cps, list):
            merged_entry["click_path_structured"] = (
                mem_cps if len(mem_cps) >= len(disk_cps) else disk_cps
            )
        elif isinstance(mem_cps, list):
            merged_entry["click_path_structured"] = mem_cps

        # Prefer non-empty direct URL
        mem_url = mem.get("opt_out_direct_url", "")
        disk_url = disk.get("opt_out_direct_url", "")
        if mem_url.startswith("http"):
            merged_entry["opt_out_direct_url"] = mem_url
        elif disk_url.startswith("http"):
            merged_entry["opt_out_direct_url"] = disk_url

        merged.append(merged_entry)

    scouted = sum(1 for e in merged if e.get("scouted"))
    has_structured = sum(1 for e in merged
                         if isinstance(e.get("click_path_structured"), list)
                         and len(e.get("click_path_structured", [])) > 1)
    has_direct = sum(1 for e in merged
                     if str(e.get("opt_out_direct_url", "")).startswith("http"))

    header = (
        f"# Broker registry -- scouted\n"
        f"# Last updated: {TODAY}\n"
        f"# {len(merged)} total, {scouted} scouted, "
        f"{has_structured} structured paths, {has_direct} direct URLs\n\n"
    )
    body = yaml.dump(
        merged, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + body)


def append_log(record: dict) -> None:
    # Promote opt_out_direct_url to top level for easy grepping
    direct = record.get("findings", {}).get("opt_out_direct_url", "")
    if direct:
        record["opt_out_direct_url"] = direct
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


# observation extraction
def extract_observations(history) -> dict:
    """Pull structured observations from a browser-use AgentHistoryList."""
    urls = [u for u in history.urls() if u]
    actions = history.action_names()
    extracted = history.extracted_content()

    thoughts = []
    for brain in history.model_thoughts():
        parts = []
        if brain.thinking:
            parts.append(f"Thinking: {brain.thinking}")
        if brain.memory:
            parts.append(f"Memory: {brain.memory}")
        if brain.evaluation_previous_goal:
            parts.append(f"Eval: {brain.evaluation_previous_goal}")
        if brain.next_goal:
            parts.append(f"Next goal: {brain.next_goal}")
        if parts:
            thoughts.append(" | ".join(parts))

    return {
        "urls": "\n".join(urls) if urls else "none",
        "actions": "\n".join(str(a) for a in actions) if actions else "none",
        "observations": "\n\n".join(thoughts) if thoughts else "none",
        "extracted": "\n".join(extracted) if extracted else "none",
    }


# synthesis call
def synthesize(
    client: anthropic.Anthropic,
    obs: dict,
    name: str,
    url: str,
    category: str,
) -> str:
    """Single Sonnet call to produce structured output from observations."""
    prompt = SYNTH_PROMPT.format(
        name=name,
        url=url,
        category=category,
        urls=obs["urls"][:2000],
        actions=obs["actions"][:2000],
        observations=obs["observations"][:4000],
        extracted=obs["extracted"][:2000],
    )
    msg = client.messages.create(
        model=MODEL_SYNTH,
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def synthesize_verify(
    client: anthropic.Anthropic,
    obs: dict,
    name: str,
    url: str,
    rep_method: str,
    rep_path: str,
) -> str:
    """Sonnet synthesis for cluster verification."""
    prompt = VERIFY_SYNTH_PROMPT.format(
        name=name,
        url=url,
        rep_method=rep_method,
        rep_path=rep_path,
        observations=obs["observations"][:3000],
    )
    msg = client.messages.create(
        model=MODEL_SYNTH,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# parsing
def parse_full(text: str) -> dict:
    import json as _json

    result = {}
    scalar_fields = [
        "IS_LIVE", "METHOD", "REQUIRES_LISTING_URL", "ID_REQUIRED",
        "CAPTCHA", "EMAIL_CONFIRMATION", "CLICK_PATH",
        "OPT_OUT_DIRECT_URL", "DIFFICULTY", "PARENT_BRAND", "NOTES",
    ]

    # Parse scalar fields with single-line regex
    for field in scalar_fields:
        m = re.search(rf"^{field}:\s*(.+)", text, re.MULTILINE | re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if val.lower() in ("true", "yes"):
                val = True
            elif val.lower() in ("false", "no"):
                val = False
            result[field.lower()] = val

    # Parse CLICK_PATH_STRUCTURED separately -- may span multiple lines
    # Try to find a JSON array anywhere after CLICK_PATH_STRUCTURED:
    cps_match = re.search(
        r"CLICK_PATH_STRUCTURED:\s*(\[.*?\])",
        text,
        re.IGNORECASE | re.DOTALL
    )
    if cps_match:
        raw_json = cps_match.group(1).strip()
        try:
            parsed = _json.loads(raw_json)
            result["click_path_structured"] = parsed
        except Exception:
            # Try cleaning common issues -- single quotes, trailing commas
            try:
                cleaned = raw_json.replace("'", "\"")
                cleaned = re.sub(r",\s*]", "]", cleaned)
                parsed = _json.loads(cleaned)
                result["click_path_structured"] = parsed
            except Exception:
                result["click_path_structured"] = raw_json
    else:
        result["click_path_structured"] = []

    return result


def parse_verify(text: str) -> dict:
    result = {}
    for field in ["CONFIRMED", "DEAD", "DIFFERENCES", "NOTES"]:
        m = re.search(rf"^{field}:\s*(.+)", text, re.MULTILINE | re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if val.lower() in ("true", "yes"):
                val = True
            elif val.lower() in ("false", "no"):
                val = False
            result[field.lower()] = val
    return result


# apply findings
def clean(s: str) -> str:
    return str(s).encode("ascii", "ignore").decode("ascii")


def apply_full(entry: dict, findings: dict, tier: str) -> dict:
    u = dict(entry)
    if findings.get("method") and findings["method"] not in ("unknown", False, True):
        u["method"] = findings["method"]
    if "requires_listing_url" in findings:
        v = findings["requires_listing_url"]
        u["requires_listing_url"] = v if isinstance(v, bool) else v is True
    if "id_required" in findings:
        v = findings["id_required"]
        u["id_required"] = v if isinstance(v, bool) else v is True
    if findings.get("difficulty") and findings["difficulty"] not in (False, True):
        u["difficulty"] = findings["difficulty"]

    u["last_checked"] = TODAY
    u["scouted"] = True
    u["scout_tier"] = tier

    # status: verified only when we actually have a usable opt-out flow
    is_live = findings.get("is_live")
    has_method = findings.get("method") not in (None, "unknown", False, True, "")
    if is_live in ("no", "error", False):
        u["status"] = recon.STATUS_BLOCKED  # reached synth but no live opt-out
    elif findings.get("id_required") is True:
        u["status"] = recon.STATUS_NEEDS_HUMAN
        u["last_verified"] = TODAY
    elif has_method:
        u["status"] = recon.STATUS_VERIFIED
        u["last_verified"] = TODAY          # flow confirmed working today
    else:
        u["status"] = recon.STATUS_NEEDS_HUMAN

    # click_path -- human readable
    cp = findings.get("click_path", "")
    if cp and str(cp).lower() not in ("none", "unknown", "false", "true", ""):
        u["click_path"] = clean(str(cp))
    else:
        u["click_path"] = u.get("click_path", "")

    # click_path_structured -- JSON action sequence
    cps = findings.get("click_path_structured", "")
    if cps and str(cps).lower() not in ("none", "unknown", "false", "true", ""):
        try:
            import json as _json
            # Parse to validate, store as native list for clean YAML output
            parsed = _json.loads(str(cps))
            u["click_path_structured"] = parsed
        except Exception:
            # If JSON parse fails store as raw string for manual review
            u["click_path_structured"] = clean(str(cps))
    else:
        u["click_path_structured"] = u.get("click_path_structured", [])

    # opt_out_direct_url -- the actual form URL, not the homepage
    direct = findings.get("opt_out_direct_url", "")
    if direct and str(direct).lower() not in ("none", "same", "false", "true", ""):
        u["opt_out_direct_url"] = clean(str(direct))
    elif str(direct).lower() == "same":
        u["opt_out_direct_url"] = u.get("opt_out_url", "")
    else:
        u["opt_out_direct_url"] = u.get("opt_out_direct_url", "")

    # notes contains everything except click_path
    parts = []
    if findings.get("captcha") is True:
        parts.append("CAPTCHA present")
    if findings.get("email_confirmation") is True:
        parts.append("Email confirmation required")
    if findings.get("is_live") in ("no", "error", False):
        parts.append("PAGE DEAD OR INACCESSIBLE")
    pb = findings.get("parent_brand", "")
    if pb and str(pb).lower() not in ("none", "false", "true", ""):
        parts.append(f"Brand: {pb}")
    n = findings.get("notes", "")
    if n and str(n).lower() not in ("none", "false", "true", ""):
        parts.append(str(n))

    u["notes"] = clean(" | ".join(parts))
    return u


def apply_verify(entry: dict, findings: dict, rep: dict) -> dict:
    u = dict(entry)
    u["last_checked"] = TODAY
    u["scouted"] = True
    u["scout_tier"] = "cluster_verify"

    dead = findings.get("dead")
    confirmed = findings.get("confirmed")

    if dead is True or str(dead).lower() == "yes":
        u["notes"] = clean("PAGE DEAD OR INACCESSIBLE | Cluster verification")
        u["difficulty"] = "manual_only"
        u["status"] = recon.STATUS_DEAD
        return u

    if confirmed is True or str(confirmed).lower() == "yes":
        for f in ["method", "requires_listing_url", "id_required", "difficulty", "click_path"]:
            if rep.get(f) is not None:
                u[f] = rep[f]
        u["notes"] = clean((rep.get("notes", "") + " | Verified cluster member"))
        u["status"] = recon.STATUS_VERIFIED
        u["last_verified"] = TODAY
        return u

    diff = findings.get("differences", "")
    n = findings.get("notes", "")
    u["notes"] = clean(f"NEEDS_FULL_SCOUT | Verify failed: {diff} {n}")
    u["status"] = recon.STATUS_UNSCOUTED
    u["scouted"] = False
    return u


def apply_dead(entry: dict, reason: str, status: str = None) -> dict:
    u = dict(entry)
    status = status or recon.classify_failure(reason)
    if status == recon.STATUS_BLOCKED:
        # anti-bot wall, not gone. Keep method/url; flag for residential-proxy retry.
        u["notes"] = clean(f"BLOCKED (likely anti-bot, retry w/ residential proxy) | {reason}")
        u["status"] = recon.STATUS_BLOCKED
    else:
        u["notes"] = clean(f"PAGE DEAD OR INACCESSIBLE | {reason}")
        u["status"] = recon.STATUS_DEAD
        u["difficulty"] = "manual_only"
    u["last_checked"] = TODAY          # we tried today
    # NOTE: last_verified is intentionally NOT touched -- the flow was not confirmed
    u["scouted"] = True
    u["scout_tier"] = "preflight"
    return u


# recon helpers
def write_osint(name: str, url: str, signals: dict, siblings: list) -> None:
    rec = {"name": name, "url": url, "timestamp": TODAY,
           "signals": signals, "siblings": siblings}
    with open(OSINT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def write_candidates(siblings: list, source_name: str, known: set) -> int:
    """Append newly discovered sibling domains to the candidate file. Returns count added."""
    new = [s for s in siblings if s not in known]
    if not new:
        return 0
    existing = []
    if CANDIDATES_OUT.exists():
        try:
            existing = yaml.safe_load(CANDIDATES_OUT.read_text()) or []
        except Exception:
            existing = []
    seen = {e.get("domain") for e in existing}
    for s in new:
        if s not in seen:
            existing.append({"domain": s, "found_via": source_name,
                             "found_on": TODAY, "scouted": False})
            seen.add(s)
            known.add(s)
    CANDIDATES_OUT.write_text(
        yaml.safe_dump(existing, sort_keys=False, allow_unicode=True))
    return len(new)


def apply_recon_email(entry: dict, rec, tier: str, note_extra: str = "") -> dict:
    """Resolve an email-only broker from recon with no browser/LLM call."""
    u = dict(entry)
    u["method"] = "email"
    u["opt_out_direct_url"] = rec.opt_out_email and f"mailto:{rec.opt_out_email}" or u.get("opt_out_url", "")
    u["difficulty"] = u.get("difficulty") or "low"
    u["status"] = recon.STATUS_VERIFIED
    u["scouted"] = True
    u["scout_tier"] = f"recon_{tier}"
    u["last_checked"] = TODAY
    u["last_verified"] = TODAY
    parts = [f"Opt-out via email: {rec.opt_out_email}", "Resolved by recon (no browser)"]
    if note_extra:
        parts.append(note_extra)
    u["notes"] = clean(" | ".join(parts))
    return u


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")[:60]


async def shoot(url: str, name: str) -> str:
    """Best-effort screenshot of an opt-out page. Returns relative path or ''."""
    if not url or not url.startswith("http"):
        return ""
    out = SHOTS_DIR / f"{slug(name)}.png"
    path = await recon.capture_screenshot(url, str(out))
    return str(Path("screenshots") / out.name) if path else ""


# preflight
async def preflight(url: str) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(
            timeout=12,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        ) as client:
            r = await client.head(url)
            if r.status_code >= 400:
                return False, f"HTTP {r.status_code}"
            return True, "ok"
    except httpx.ConnectError:
        return False, "connection refused"
    except httpx.TimeoutException:
        return False, "timeout"
    except Exception as e:
        return False, str(e)[:80]


# tier assignment
def assign_tier(entry: dict, reps_scouted: set) -> str:
    cluster = entry.get("parent_cluster")
    name = entry.get("name", "")
    if cluster:
        rep = CLUSTER_REPS.get(cluster)
        if name == rep or rep not in reps_scouted:
            return "cluster_rep"
        return "cluster_verify"
    return "standard"


# browser agent runner
async def run_nav(task: str, llm_haiku, llm_sonnet=None) -> object:
    profile = BrowserProfile(
        headless=True,
        keep_alive=False,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        # Balanced wait times -- enough for page load, not so much we stall
        minimum_wait_page_load_time=0.5,
        wait_for_network_idle_page_load_time=1.0,
        wait_between_actions=0.2,
    )

    tab_handling_instructions = """
IMPORTANT TAB AND POPUP HANDLING:
- If a new tab opens after clicking a link, wait for it to fully load before acting.
- If you see a cookie consent, privacy popup, or consent management platform (OneTrust,
  TrustArc, etc.) on any page including new tabs, dismiss it first by clicking
  "Reject All", "Decline", or "Close" before continuing.
- If a page appears blank or is still loading, use scroll_down to trigger rendering.
- Always record the current URL in your memory after any navigation.
- If you are on a new tab, check the URL before acting to confirm the page loaded.
"""

    agent = Agent(
        task=task,
        llm=llm_haiku,
        fallback_llm=llm_sonnet,
        browser_profile=profile,
        max_failures=3,
        use_vision=True,
        use_judge=False,
        max_actions_per_step=5,
        extend_system_message=tab_handling_instructions,
        step_timeout=30,
    )
    return await agent.run(max_steps=15)


# scout one broker
async def scout_one(
    entry: dict,
    tier: str,
    llm_haiku,
    llm_sonnet,
    synth_client: anthropic.Anthropic,
    cluster_rep_findings: dict,
    do_preflight: bool,
    refresh_urls: bool = False,
    do_recon: bool = True,
    do_osint: bool = False,
    do_shots: bool = True,
    known_domains: set = None,
    cc_driver=None,
) -> tuple[dict, dict]:
    name = entry.get("name", "Unknown")
    url  = entry.get("opt_out_url", "")
    cat  = entry.get("category", "")

    print(f"\n{'='*55}")
    print(f"[{entry.get('sensitivity','?')}] {name}  ({tier})")
    print(f"    {url}")

    log = {
        "name": name, "url": url, "category": cat,
        "tier": tier, "timestamp": TODAY,
        "findings": {}, "error": None,
    }

    if not url or url in ("unknown", ""):
        log["error"] = "No URL"
        entry["notes"] = "No opt-out URL available"
        entry["scouted"] = True
        entry["scout_tier"] = "skipped"
        return entry, log

    # URL refresh mode
    # Full synthesis pass -- gets direct URL + structured path + any updated findings
    if refresh_urls:
        task = URL_REFRESH_TASK.format(
            url=url, name=name,
            click_path=entry.get("click_path", "navigate to opt-out page")
        )
        try:
            if cc_driver is not None:
                from .claude_code_driver import synthesize_claude_code
                obs = await cc_driver.run(url, name, task)
                raw = await synthesize_claude_code(obs, name, url, cat)
            else:
                history = await run_nav(task, llm_haiku, llm_sonnet)
                obs = extract_observations(history)
                raw = synthesize(synth_client, obs, name, url, cat)
            findings = parse_full(raw)
            log["findings"] = findings
            log["raw_synth"] = raw
            log["observations"] = obs

            # Apply all findings but preserve existing values where new ones are empty
            # (we already have good click_path text from original scout)
            updated = apply_full(entry, findings, "refresh")

            # If synthesizer did not produce a better click_path keep the original
            if not updated.get("click_path") and entry.get("click_path"):
                updated["click_path"] = entry["click_path"]

            print(
                f"    direct_url={updated.get('opt_out_direct_url','?')[:60]}  "
                f"structured_steps={len(updated.get('click_path_structured') or [])}"
            )
            return updated, log
        except Exception as e:
            err = str(e)[:200]
            print(f"    URL REFRESH ERROR: {err}")
            log["error"] = err
            return entry, log

    # Tier 0: preflight
    if do_preflight:
        live, reason = await preflight(url)
        if not live:
            cls = recon.classify_failure(reason)
            if cls == recon.STATUS_DEAD:
                print(f"    DEAD ({reason})")
                log["findings"] = {"is_live": False, "reason": reason}
                return apply_dead(entry, reason, status=recon.STATUS_DEAD), log
            # Blocked: a bare HEAD often 403s on Cloudflare even when the page is
            # fine. Don't write it off -- recon GET (full UA) and the browser get a shot.
            print(f"    preflight blocked ({reason}); continuing to recon/browser")
            log["preflight_blocked"] = reason

    try:
        # Recon pass (cheap HTTP, before any browser/LLM)
        rec = None
        sig = {}
        if do_recon:
            data = await asyncio.to_thread(
                recon.recon_and_fingerprint, url,
                recon._default_fetcher, do_osint, known_domains or set())
            rec = data["recon"]
            sig = data["signals"]
            siblings = data["siblings"]
            log["recon"] = {"method": rec.method, "opt_out_url": rec.opt_out_url,
                            "status": rec.status, "confidence": rec.confidence}
            log["signals"] = sig
            if sig or siblings:
                write_osint(name, url, sig, siblings)
            if siblings:
                n_new = write_candidates(siblings, f"sibling_of:{name}", known_domains or set())
                print(f"    OSINT: +{n_new} sibling candidates")
            if rec.status == recon.STATUS_BLOCKED:
                # httpx got walled, but browser-use drives real Chromium and often
                # gets through Cloudflare. Note it and let the browser try.
                print(f"    recon blocked ({rec.reason}); browser will try")
                log["recon_blocked"] = rec.reason

        # Cluster verification
        if tier == "cluster_verify":
            cluster = entry.get("parent_cluster")
            rep = cluster_rep_findings.get(cluster, {})
            if not rep:
                print(f"    No rep for {cluster} -- running as standard")
                tier = "standard"
            else:
                # Fingerprint match: identical infra to the rep => confirm, no browser.
                match = recon.cluster_match(sig, rep.get("signals", {})) if sig else (0.0, [])
                if match[0] >= 0.6:
                    updated = apply_verify(entry, {"confirmed": "yes"}, rep)
                    updated["notes"] = clean(
                        rep.get("notes", "") +
                        f" | Verified by fingerprint {match[0]:.2f} ({';'.join(match[1][:2])})")
                    print(f"    cluster confirmed by fingerprint {match[0]:.2f} (no browser)")
                    return updated, log
                # otherwise fall back to the browser verify (seeded if recon found a url)
                seed_url = rec.opt_out_url if (rec and rec.opt_out_url) else url
                task = VERIFY_TASK.format(
                    url=seed_url, name=name,
                    rep_method=rep.get("method", "unknown"),
                    rep_path=rep.get("click_path", "unknown"),
                    rep_difficulty=rep.get("difficulty", "unknown"),
                )
                if cc_driver is not None:
                    from .claude_code_driver import synthesize_claude_code
                    obs = await cc_driver.run(seed_url, name, task)
                    raw = await synthesize_claude_code(obs, name, url, cat)
                    findings = parse_full(raw)
                else:
                    history = await run_nav(task, llm_haiku, llm_sonnet)
                    obs = extract_observations(history)
                    raw = synthesize_verify(
                        synth_client, obs, name, url,
                        rep.get("method", ""), rep.get("click_path", "")
                    )
                    findings = parse_verify(raw)
                log["findings"] = findings
                log["raw_synth"] = raw
                updated = apply_verify(entry, findings, rep)
                print(f"    confirmed={findings.get('confirmed')}  dead={findings.get('dead')}")
                return updated, log

        # Recon email short-circuit (no browser/LLM needed)
        if rec and rec.method == "email" and rec.short_circuit:
            updated = apply_recon_email(entry, rec, tier,
                                        note_extra=recon.notes_summary(sig, []))
            if do_shots:
                shot = await shoot(rec.opt_out_url or url, name)
                if shot:
                    updated["screenshot"] = shot
            print(f"    email opt-out via recon: {rec.opt_out_email} (no browser)")
            return updated, log

        # Full scout (standard + cluster_rep)
        # Seed the browser with the recon opt-out URL so nav is 2-3 steps, not 15.
        start_url = rec.opt_out_url if (rec and rec.opt_out_url) else url
        task = NAV_TASK.format(url=start_url, name=name)
        if cc_driver is not None:
            from .claude_code_driver import synthesize_claude_code
            obs = await cc_driver.run(start_url, name, task)
            raw = await synthesize_claude_code(obs, name, start_url, cat)
        else:
            history = await run_nav(task, llm_haiku, llm_sonnet)
            obs = extract_observations(history)
            raw = synthesize(synth_client, obs, name, start_url, cat)
        findings = parse_full(raw)
        log["findings"] = findings
        log["raw_synth"] = raw
        log["observations"] = obs

        updated = apply_full(entry, findings, tier)

        # fold recon fingerprint signals into notes
        if sig:
            rn = recon.notes_summary(sig, [])
            if rn:
                updated["notes"] = clean((updated.get("notes", "") + " | " + rn).strip(" |"))

        # screenshot the opt-out page we landed on
        if do_shots:
            shot_url = updated.get("opt_out_direct_url") or start_url
            shot = await shoot(shot_url, name)
            if shot:
                updated["screenshot"] = shot

        # Store rep findings (including fingerprint signals) for the cluster
        cluster = entry.get("parent_cluster")
        if cluster and tier == "cluster_rep":
            cluster_rep_findings[cluster] = {
                "method": updated.get("method"),
                "click_path": updated.get("click_path", ""),
                "difficulty": updated.get("difficulty"),
                "notes": updated.get("notes", ""),
                "requires_listing_url": updated.get("requires_listing_url"),
                "id_required": updated.get("id_required"),
                "signals": sig,
            }
            print(f"    Stored rep for cluster: {cluster}")

        print(
            f"    live={findings.get('is_live')}  "
            f"method={findings.get('method')}  "
            f"difficulty={findings.get('difficulty')}  "
            f"id={findings.get('id_required')}  status={updated.get('status')}"
        )
        return updated, log

    except FatalAPIError:
        raise
    except Exception as e:
        err = str(e)[:300]
        if _is_fatal_api_error(err):
            # billing/auth -- do NOT mark scouted; halt so the run can resume later.
            print(f"    FATAL API ERROR: {err}")
            raise FatalAPIError(err)
        print(f"    ERROR: {err}")
        log["error"] = err
        entry["notes"] = clean(f"Scout error: {err}")
        entry["status"] = recon.STATUS_UNSCOUTED
        entry["scouted"] = False   # transient -- retry next run rather than look done
        entry["scout_tier"] = tier
        return entry, log


# main
async def main():
    parser = argparse.ArgumentParser(description="Scout data broker opt-out pages")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--category", type=str, default=None)
    parser.add_argument("--sensitivity-min", type=int, default=0)
    parser.add_argument("--skip-scouted", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-preflight", action="store_true")
    parser.add_argument("--no-cluster-opt", action="store_true")
    parser.add_argument("--refresh-urls", action="store_true",
                        help="Re-visit scouted entries missing opt_out_direct_url")
    parser.add_argument("--no-recon", action="store_true",
                        help="Disable the cheap HTTP recon pre-pass")
    parser.add_argument("--osint", action="store_true",
                        help="Enable crt.sh sibling discovery (writes discovered_candidates.yaml)")
    parser.add_argument("--no-shots", action="store_true",
                        help="Disable opt-out page screenshots")
    parser.add_argument("--nav-model", type=str, default=MODEL_NAV,
                        help=f"navigation model (default {MODEL_NAV})")
    parser.add_argument("--synth-model", type=str, default=MODEL_SYNTH,
                        help=f"synthesis model (default {MODEL_SYNTH})")
    parser.add_argument("--claude-code", action="store_true",
                        help="Use Claude Code (you, the agent) as the navigator+synthesizer "
                             "instead of the Anthropic API. No API key required.")

    args = parser.parse_args()

    source = REGISTRY_OUT if REGISTRY_OUT.exists() else REGISTRY_IN
    all_entries = load_registry(source)
    print(f"Loaded {len(all_entries)} entries from {source.name}")

    work = list(all_entries)
    if args.category:
        work = [e for e in work if e.get("category") == args.category]
        print(f"Category '{args.category}': {len(work)}")
    if args.sensitivity_min > 0:
        work = [e for e in work if int(e.get("sensitivity", 0)) >= args.sensitivity_min]
        print(f"Sensitivity >= {args.sensitivity_min}: {len(work)}")
    if args.skip_scouted:
        work = [e for e in work if not e.get("scouted")]
        print(f"Skip scouted: {len(work)} remaining")
    if args.refresh_urls:
        work = [e for e in work if e.get("scouted") and not e.get("opt_out_direct_url")
                and e.get("click_path") and "Scout error" not in e.get("click_path","")
                and "No opt-out" not in e.get("click_path","")]
        print(f"Refresh URLs mode: {len(work)} entries need direct URL")

    end = args.end if args.end is not None else len(work)
    work = work[args.start:end]

    # Determine which cluster reps are already scouted
    reps_scouted = set()
    if not args.no_cluster_opt:
        for e in all_entries:
            if e.get("scouted") and e.get("scout_tier") == "cluster_rep":
                reps_scouted.add(e.get("name"))

    # Assign tiers and sort (reps first, then standard, then verify)
    tier_order = {"cluster_rep": 0, "standard": 1, "cluster_verify": 2}
    tiered = []
    tier_counts = defaultdict(int)
    for entry in work:
        tier = "standard" if args.no_cluster_opt else assign_tier(entry, reps_scouted)
        tiered.append((entry, tier))
        tier_counts[tier] += 1
    tiered.sort(key=lambda x: tier_order.get(x[1], 99))

    print(f"\nWill scout {len(tiered)} entries (index {args.start}:{end})")
    for tier, count in sorted(tier_counts.items()):
        print(f"  {tier:<20} {count}")

    if args.dry_run:
        for entry, tier in tiered:
            print(
                f"  [{entry.get('sensitivity','?'):>2}] "
                f"{entry.get('name','?'):<40} "
                f"[{tier}]"
            )
        return

    if args.claude_code:
        from .claude_code_driver import ClaudeCodeDriver
        llm_haiku = llm_sonnet = synth_client = None
        cc_driver = ClaudeCodeDriver(headless=False)
        print("[claude-code mode] No API key needed. "
              "I will drive the browser and synthesize findings.")
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY not set. "
                  "Add --claude-code to run without an API key.")
            sys.exit(1)
        # Navigation now defaults to Sonnet (Haiku underperformed per the logs).
        llm_haiku   = ChatAnthropic(model=args.nav_model,   api_key=api_key, max_tokens=2000)
        llm_sonnet  = ChatAnthropic(model=args.synth_model, api_key=api_key, max_tokens=2000)
        synth_client = anthropic.Anthropic(api_key=api_key)
        cc_driver = None

    # Known registrable domains -> dedup target for OSINT sibling discovery.
    known_domains = set()
    for e in all_entries:
        d = recon.registrable(e.get("domain") or e.get("opt_out_url") or "")
        if d:
            known_domains.add(d)

    name_to_idx = {e.get("name"): i for i, e in enumerate(all_entries)}

    # Load existing cluster rep findings from already-scouted entries
    cluster_rep_findings = {}
    for e in all_entries:
        if e.get("scout_tier") == "cluster_rep" and e.get("scouted"):
            cluster = e.get("parent_cluster")
            if cluster and cluster not in cluster_rep_findings:
                cluster_rep_findings[cluster] = {
                    "method": e.get("method"),
                    "click_path": "",
                    "difficulty": e.get("difficulty"),
                    "notes": e.get("notes", ""),
                    "requires_listing_url": e.get("requires_listing_url"),
                    "id_required": e.get("id_required"),
                }

    completed = 0
    flags = []

    for i, (entry, tier) in enumerate(tiered):
        print(f"\n[{i+1}/{len(tiered)}]", end="")
        try:
            updated, log = await asyncio.wait_for(
                scout_one(
                    entry=entry,
                    tier=tier,
                    llm_haiku=llm_haiku,
                    llm_sonnet=llm_sonnet,
                    synth_client=synth_client,
                    cluster_rep_findings=cluster_rep_findings,
                    do_preflight=not args.no_preflight,
                    refresh_urls=args.refresh_urls,
                    do_recon=not args.no_recon,
                    do_osint=args.osint,
                    do_shots=not args.no_shots,
                    known_domains=known_domains,
                    cc_driver=cc_driver,
                ),
                timeout=1800 if args.claude_code else 300,
            )
        except FatalAPIError as e:
            print(f"\n{'!'*55}")
            print(f"HALTING: fatal API error ({e}).")
            print(f"Progress saved. Fix billing/key, then re-run with --skip-scouted to resume.")
            save_registry(all_entries, REGISTRY_OUT)
            sys.exit(2)
        except asyncio.TimeoutError:
            name = entry.get("name", "unknown")
            print(f"\n    TIMEOUT after 5 minutes -- skipping {name}")
            entry["notes"] = "Scout timeout after 5 minutes"
            entry["status"] = recon.STATUS_BLOCKED
            entry["last_checked"] = TODAY
            entry["scouted"] = True
            entry["scout_tier"] = "timeout"
            updated = entry
            log = {"name": name, "error": "timeout", "timestamp": TODAY}

        name = entry.get("name")
        if name in name_to_idx:
            all_entries[name_to_idx[name]] = updated

        append_log(log)
        save_registry(all_entries, REGISTRY_OUT)
        completed += 1

        notes = updated.get("notes", "").upper()
        if updated.get("id_required") is True:
            flags.append(("ID_REQUIRED", name))
        if updated.get("difficulty") == "manual_only":
            flags.append(("MANUAL_ONLY", name))
        if "DEAD" in notes:
            flags.append(("PAGE_DEAD", name))
        if "BLOCKED" in notes:
            flags.append(("BLOCKED_RETRY_PROXY", name))
        if "NEEDS_FULL_SCOUT" in notes:
            flags.append(("NEEDS_FULL_SCOUT", name))

    print(f"\n{'='*55}")
    print(f"Done. {completed}/{len(tiered)} brokers processed.")
    print(f"Registry: {REGISTRY_OUT}")
    print(f"Log:      {LOG_FILE}")
    if CANDIDATES_OUT.exists():
        try:
            cands = yaml.safe_load(CANDIDATES_OUT.read_text()) or []
            unscouted = sum(1 for c in cands if not c.get("scouted"))
            print(f"OSINT candidates: {len(cands)} discovered ({unscouted} new) -> {CANDIDATES_OUT.name}")
        except Exception:
            pass

    if flags:
        print(f"\nFlags ({len(flags)}):")
        for flag_type, fname in flags:
            print(f"  {flag_type}: {fname}")


if __name__ == "__main__":
    asyncio.run(main())

# adapter for databroker.stages.scout._deep_scout
def scout_url(url: str, name: str, cfg) -> dict:
    """Single-URL deep scout used by the pipeline. Runs the browser nav +
    synthesis for one broker and returns the findings dict (no YAML side effects).
    Pass cfg.claude_code=True to use Claude Code instead of the Anthropic API."""
    import asyncio as _asyncio

    async def _go():
        if getattr(cfg, "claude_code", False):
            from .claude_code_driver import ClaudeCodeDriver, synthesize_claude_code
            driver = ClaudeCodeDriver(headless=False)
            task = NAV_TASK.format(url=url, name=name)
            obs = await driver.run(url, name, task)
            raw = await synthesize_claude_code(obs, name, url, "")
        else:
            llm_nav = ChatAnthropic(model=cfg.nav_model, api_key=cfg.anthropic_key, max_tokens=2000)
            synth_client = anthropic.Anthropic(api_key=cfg.anthropic_key)
            task = NAV_TASK.format(url=url, name=name)
            history = await run_nav(task, llm_nav, llm_nav)
            obs = extract_observations(history)
            raw = synthesize(synth_client, obs, name, url, "")
        return parse_full(raw)

    try:
        return _asyncio.get_event_loop().run_until_complete(_go())
    except RuntimeError:
        return _asyncio.run(_go())
