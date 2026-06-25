"""
databroker.stages.drivers -- Tier 1 execution, no LLM.

PlaywrightDriver replays a scouted recipe (click_path_structured) deterministically:
navigate, scroll, click-by-label, fill-by-field, submit. No model decides anything;
the scout already recorded the path. This is what lets someone run removals with
zero API cost.

Defaults to dry_run=True (fills the form, stops before submit, screenshots) so a
person can watch it work before letting it actually submit. Set dry_run=False to
submit for real. Both drivers are optional: import them only if you want Tier 1.
"""
from __future__ import annotations
import os


class PlaywrightDriver:
    """Deterministic recipe replay. Implements remover.BrowserDriver."""

    def __init__(self, headless: bool = False, dry_run: bool = True,
                 proxy: str | None = None, shots_dir: str = "data/screenshots"):
        # headful by default: real Chrome gets past anti-bot better than headless
        self.headless = headless
        self.dry_run = dry_run
        self.proxy = proxy
        self.shots_dir = shots_dir

    async def replay(self, start_url: str, steps: list) -> dict:
        try:
            from playwright.async_api import async_playwright
        except Exception:
            return {"ok": False, "error": "playwright not installed ([browser] extra)"}

        os.makedirs(self.shots_dir, exist_ok=True)
        result = {"ok": False, "submitted": False, "dry_run": self.dry_run,
                  "start_url": start_url, "filled": 0, "screenshot": ""}
        launch = {"headless": self.headless}
        if self.proxy:
            launch["proxy"] = {"server": self.proxy}
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch)
                page = await (await browser.new_context(
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/124.0.0.0 Safari/537.36"))).new_page()
                if start_url:
                    await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)

                for step in steps:
                    action = step.get("action")
                    if action == "navigate" and step.get("url"):
                        await page.goto(step["url"], wait_until="domcontentloaded", timeout=30000)
                    elif action == "scroll":
                        await page.mouse.wheel(0, 1200)
                    elif action == "click":
                        await self._click(page, step)
                    elif action == "fill":
                        if await self._fill(page, step):
                            result["filled"] += 1
                    elif action == "submit":
                        shot = os.path.join(self.shots_dir, "before_submit.png")
                        await page.screenshot(path=shot)
                        result["screenshot"] = shot
                        if self.dry_run:
                            result["ok"] = True
                            break
                        await self._submit(page, step)
                        result["submitted"] = True
                    await page.wait_for_timeout(400)

                result["ok"] = True
                await browser.close()
        except Exception as e:
            result["error"] = str(e)[:200]
        return result

    async def _click(self, page, step):
        label = step.get("label", "")
        # try visible-text / role first, then the recorded selector hint
        for locator in (page.get_by_role("link", name=label, exact=False),
                        page.get_by_role("button", name=label, exact=False),
                        page.get_by_text(label, exact=False)):
            try:
                if await locator.count():
                    await locator.first.click(timeout=5000)
                    return
            except Exception:
                continue
        hint = step.get("selector_hint")
        if hint:
            try:
                await page.locator(hint).first.click(timeout=5000)
            except Exception:
                pass

    async def _fill(self, page, step) -> bool:
        field, value = step.get("field", ""), step.get("value", "")
        if not value or value.startswith("{"):  # unresolved template var -> skip
            return False
        for sel in (f'input[name="{field}"]', f'textarea[name="{field}"]',
                    f'input[id="{field}"]', f'[placeholder*="{field}" i]'):
            try:
                loc = page.locator(sel)
                if await loc.count():
                    await loc.first.fill(value, timeout=5000)
                    return True
            except Exception:
                continue
        try:  # last resort: by accessible label
            await page.get_by_label(field, exact=False).first.fill(value, timeout=4000)
            return True
        except Exception:
            return False

    async def _submit(self, page, step):
        label = step.get("label", "Submit")
        for locator in (page.get_by_role("button", name=label, exact=False),
                        page.locator('button[type="submit"], input[type="submit"]')):
            try:
                if await locator.count():
                    await locator.first.click(timeout=5000)
                    return
            except Exception:
                continue


class SmtpMailDriver:
    """Send opt-out emails for email-method brokers. Implements remover.MailDriver.
    Reads SMTP settings from env: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM."""

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run

    async def send(self, to: str, subject: str, body: str) -> dict:
        if self.dry_run or not to:
            return {"ok": True, "submitted": False, "dry_run": True, "to": to}
        import smtplib, asyncio
        from email.message import EmailMessage

        def _send():
            msg = EmailMessage()
            msg["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", ""))
            msg["To"] = to
            msg["Subject"] = subject
            msg.set_content(body)
            host = os.environ["SMTP_HOST"]; port = int(os.environ.get("SMTP_PORT", "587"))
            with smtplib.SMTP(host, port) as s:
                s.starttls()
                if os.environ.get("SMTP_USER"):
                    s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
            return True

        try:
            await asyncio.to_thread(_send)
            return {"ok": True, "submitted": True, "to": to}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "to": to}
