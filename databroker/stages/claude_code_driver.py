"""
databroker.stages.claude_code_driver
-------------------------------------
Playwright browser driver that replaces browser-use + Anthropic API when
running in --claude-code mode. Claude Code (the agent in your terminal) acts
as both the navigator and the synthesizer.

Protocol (one iteration per browser step):
  1. Driver takes a screenshot, writes data/cc_request.json
  2. Claude Code reads the screenshot + request file, writes data/cc_response.json
  3. Driver reads the response, executes the action, loops

For synthesis (replacing the Sonnet API call):
  1. Driver writes data/cc_synth_request.json with full observation log
  2. Claude Code reads it, writes back data/cc_synth_response.json in the
     exact key=value text format that parse_full() already understands
  3. Driver returns that text to the caller unchanged

CAPTCHA / Cloudflare handling:
  - Driver writes action="wait_for_human" in the request
  - Browser is headless=False so the user can see and interact with it
  - After the user solves the challenge, Claude Code writes {"action": "continue"}
  - Driver re-screenshots and continues

Response file schemas
---------------------
Navigation action response (data/cc_response.json):
{
  "action": "click" | "fill" | "navigate" | "scroll" | "wait" | "done",
  "label": "...",          // for click -- visible link/button text
  "selector_hint": "...",  // for click -- CSS selector hint
  "url": "...",            // for navigate
  "field": "...",          // for fill -- label or name of the field
  "value": "...",          // for fill
  "direction": "down",     // for scroll
  "notes": "..."           // for done -- summary of what was found
}

Synthesis response (data/cc_synth_response.json) -- plain text, same format
as the SYNTH_PROMPT output so parse_full() works unchanged:

IS_LIVE: yes
METHOD: web_form
REQUIRES_LISTING_URL: false
ID_REQUIRED: false
CAPTCHA: false
EMAIL_CONFIRMATION: false
CLICK_PATH: Scroll to footer > Click "Privacy" > ...
CLICK_PATH_STRUCTURED: [{"action":"navigate","url":"..."}]
OPT_OUT_DIRECT_URL: https://...
DIFFICULTY: low
PARENT_BRAND: none
NOTES: none
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

REQUEST_FILE  = Path("data/cc_request.json")
RESPONSE_FILE = Path("data/cc_response.json")
SYNTH_REQUEST_FILE  = Path("data/cc_synth_request.json")
SYNTH_RESPONSE_FILE = Path("data/cc_synth_response.json")

POLL_INTERVAL  = 1.5   # seconds between polls
MAX_WAIT_NAV   = 600   # 10 min max per navigation step
MAX_WAIT_SYNTH = 900   # 15 min max for synthesis

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

STEPS_DIR = Path("data/cc_steps")


class ClaudeCodeDriver:
    """
    Drives Playwright step-by-step under Claude Code control.
    headless=False so the user can see the browser and handle challenges.
    """

    def __init__(self, headless: bool = False, max_steps: int = 20):
        self.headless  = headless
        self.max_steps = max_steps

    async def run(self, start_url: str, name: str, task_hint: str = "") -> dict:
        """
        Navigate from start_url to the opt-out mechanism under Claude Code
        direction. Returns an observations dict in the same shape that
        extract_observations() returns so the rest of browser_scout works
        without changes.
        """
        STEPS_DIR.mkdir(parents=True, exist_ok=True)

        try:
            from playwright.async_api import async_playwright
        except Exception:
            return {"urls": start_url, "actions": "playwright_unavailable",
                    "observations": "Playwright not installed", "extracted": ""}

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            ctx = await browser.new_context(
                user_agent=UA,
                viewport={"width": 1280, "height": 900},
            )
            page = await ctx.new_page()

            urls_visited: list[str] = []
            actions_taken: list[str] = []
            obs_log:       list[str] = []

            try:
                await page.goto(start_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                obs_log.append(f"Initial load error: {e}")

            for step in range(1, self.max_steps + 1):
                current_url = page.url
                urls_visited.append(current_url)

                shot_path = STEPS_DIR / f"{_slug(name)}_step{step:02d}.png"
                try:
                    await page.screenshot(path=str(shot_path), full_page=False)
                except Exception:
                    shot_path = Path("")

                page_text = ""
                try:
                    full_text = (await page.evaluate("document.body.innerText") or "")
                    # Include first 2000 chars AND last 1000 chars so opt-out info near
                    # the bottom of long privacy policies is always visible
                    if len(full_text) > 3000:
                        page_text = full_text[:2000] + "\n...[middle omitted]...\n" + full_text[-1000:]
                    else:
                        page_text = full_text
                except Exception:
                    pass

                request = {
                    "step": step,
                    "broker_name": name,
                    "task_hint": task_hint or f"Map the opt-out flow for {name}",
                    "current_url": current_url,
                    "screenshot": str(shot_path),
                    "page_text_excerpt": page_text,
                    "history": [
                        {"step": i + 1, "url": u, "action": a}
                        for i, (u, a) in enumerate(zip(urls_visited, actions_taken))
                    ],
                    "instructions": (
                        "Read the screenshot and page text. Decide the next single action.\n"
                        "Write your response to: " + str(RESPONSE_FILE) + "\n\n"
                        "Available actions:\n"
                        '  {"action":"click","label":"exact visible text","selector_hint":"css hint"}\n'
                        '  {"action":"fill","field":"field label or name","value":"value"}\n'
                        '  {"action":"navigate","url":"https://..."}\n'
                        '  {"action":"scroll","direction":"down"}\n'
                        '  {"action":"wait","seconds":5}\n'
                        '  {"action":"wait_for_human","reason":"cloudflare challenge"}\n'
                        '  {"action":"done","notes":"summary of what you found"}\n\n'
                        "Goal: reach the actual opt-out form or mechanism. "
                        "Stop (done) once you have found it or confirmed it does not exist."
                    ),
                }
                REQUEST_FILE.write_text(json.dumps(request, indent=2, ensure_ascii=False), encoding="utf-8")

                print(f"\n[cc_driver] step {step}/{self.max_steps}  url={current_url}")
                print(f"[cc_driver] screenshot -> {shot_path}")
                print(f"[cc_driver] WAITING FOR CLAUDE CODE -> write {RESPONSE_FILE}")

                response = await _poll(RESPONSE_FILE, MAX_WAIT_NAV)
                if response is None:
                    obs_log.append(f"Step {step}: timed out waiting for Claude Code response")
                    print("[cc_driver] timed out -- stopping navigation")
                    break

                action = response.get("action", "")
                actions_taken.append(json.dumps(response))
                obs_log.append(f"Step {step}: {current_url} | {action} | {json.dumps(response)}")

                if action == "done":
                    obs_log.append(f"Navigation done: {response.get('notes', '')}")
                    break

                elif action == "wait_for_human":
                    reason = response.get("reason", "challenge")
                    print(f"\n[cc_driver] *** HUMAN NEEDED: {reason} ***")
                    print("[cc_driver] Solve the challenge in the browser window,")
                    print("[cc_driver] then write {\"action\":\"continue\"} to " + str(RESPONSE_FILE))
                    ack = await _poll(RESPONSE_FILE, MAX_WAIT_NAV)
                    if ack:
                        obs_log.append(f"Step {step}: human resolved challenge ({reason})")
                    continue

                elif action == "navigate":
                    try:
                        await page.goto(response.get("url", current_url),
                                        wait_until="domcontentloaded", timeout=30_000)
                    except Exception as e:
                        obs_log.append(f"Step {step}: navigate error {e}")

                elif action == "click":
                    label = response.get("label", "")
                    hint  = response.get("selector_hint", "")
                    clicked = False
                    if label:
                        try:
                            await page.get_by_text(label, exact=False).first.click(timeout=5_000)
                            clicked = True
                        except Exception:
                            pass
                    if not clicked and hint:
                        try:
                            await page.locator(hint).first.click(timeout=5_000)
                            clicked = True
                        except Exception:
                            pass
                    if not clicked:
                        obs_log.append(f"Step {step}: could not click '{label}' / '{hint}'")
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    except Exception:
                        pass

                elif action == "fill":
                    field = response.get("field", "")
                    value = response.get("value", "")
                    filled = False
                    if field:
                        for locator in [
                            page.get_by_label(field, exact=False),
                            page.locator(f"input[name*='{field}']"),
                            page.locator(f"input[placeholder*='{field}']"),
                        ]:
                            try:
                                await locator.first.fill(value, timeout=3_000)
                                filled = True
                                break
                            except Exception:
                                pass
                    if not filled:
                        obs_log.append(f"Step {step}: could not fill field '{field}'")

                elif action == "scroll":
                    direction = response.get("direction", "down")
                    amount = 600 if direction == "down" else -600
                    try:
                        await page.evaluate(f"window.scrollBy(0, {amount})")
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)

                elif action == "wait":
                    secs = float(response.get("seconds", 3))
                    await asyncio.sleep(min(secs, 30))

            await browser.close()

        # Clean up request file
        try:
            REQUEST_FILE.unlink(missing_ok=True)
        except Exception:
            pass

        return {
            "urls":         "\n".join(urls_visited),
            "actions":      "\n".join(actions_taken),
            "observations": "\n\n".join(obs_log),
            "extracted":    "",
        }


async def synthesize_claude_code(obs: dict, name: str, url: str, category: str) -> str:
    """
    Ask Claude Code to synthesize browser observations into structured findings.
    Writes data/cc_synth_request.json; waits for data/cc_synth_response.json.
    Returns raw text in the same format as SYNTH_PROMPT output so parse_full()
    works unchanged.
    """
    request = {
        "type": "synthesis",
        "broker_name": name,
        "url": url,
        "category": category,
        "observations": obs,
        "instructions": (
            "Analyze the browser session below and write your findings to:\n"
            + str(SYNTH_RESPONSE_FILE) + "\n\n"
            "The response must be plain text in EXACTLY this format "
            "(one item per line, no extra text):\n\n"
            "IS_LIVE: yes\n"
            "METHOD: web_form\n"
            "REQUIRES_LISTING_URL: false\n"
            "ID_REQUIRED: false\n"
            "CAPTCHA: false\n"
            "EMAIL_CONFIRMATION: false\n"
            "CLICK_PATH: Scroll to footer > Click ...\n"
            'CLICK_PATH_STRUCTURED: [{"action":"navigate","url":"..."}, ...]\n'
            "OPT_OUT_DIRECT_URL: https://...\n"
            "DIFFICULTY: low\n"
            "PARENT_BRAND: none\n"
            "NOTES: none\n\n"
            "Valid METHOD values: web_form | email | phone | mail | api | manual_only | unknown\n"
            "Valid DIFFICULTY values: low | medium | high | manual_only\n"
        ),
    }
    SYNTH_REQUEST_FILE.write_text(json.dumps(request, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[cc_driver] SYNTHESIS REQUEST -> write {SYNTH_RESPONSE_FILE}")

    raw = await _poll_text(SYNTH_RESPONSE_FILE, MAX_WAIT_SYNTH)
    try:
        SYNTH_REQUEST_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    if raw is None:
        print("[cc_driver] synthesis timed out -- returning empty findings")
        return "IS_LIVE: yes\nMETHOD: unknown\nDIFFICULTY: manual_only\nNOTES: synthesis timed out"
    return raw


# helpers

def _slug(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", (name or "broker").lower()).strip("_")[:40]


async def _poll(path: Path, timeout: float) -> dict | None:
    """Poll for a JSON file. Returns parsed dict or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                raw = path.read_bytes()
                if not raw:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                data = json.loads(raw.decode("utf-8", errors="replace"))
                path.unlink(missing_ok=True)
                return data
            except Exception:
                pass
        await asyncio.sleep(POLL_INTERVAL)
    return None


async def _poll_text(path: Path, timeout: float) -> str | None:
    """Poll for any text file. Returns content string or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                raw = path.read_bytes()
                if not raw:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                text = raw.decode("utf-8", errors="replace")
                path.unlink(missing_ok=True)
                return text
            except Exception:
                pass
        await asyncio.sleep(POLL_INTERVAL)
    return None
