from __future__ import annotations
from typing import Dict, Any, Optional, List
import logging, re, time
from modules.base import OutreachChannel

log = logging.getLogger("outreach.forms")

# Простая эвристика: передаём в контексте список URL форм и поля для заполнения
def _form_urls(ctx: Dict[str, Any]) -> List[str]:
    urls = ctx.get("contact_form_urls") or []
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.split(",") if u.strip()]
    return [u for u in urls if u.startswith("http")]

def _form_fields(ctx: Dict[str, Any]) -> Dict[str, str]:
    # минимальный набор; при необходимости расширишь маппинг названий
    msg = ctx.get("form_message") or \
          f"Здравствуйте! Интересно сотрудничество. Письмо: { (ctx.get('emails') or [''])[0] }"
    return {
        "email": (ctx.get("reply_email") or (ctx.get("emails") or [""])[0] or "").strip(),
        "message": msg[:2000]
    }

class FormsChannel(OutreachChannel):
    kind = "form"

    def available(self, ctx: Dict[str, Any]) -> bool:
        return len(_form_urls(ctx)) > 0

    def build_job(self, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        urls = _form_urls(ctx)
        fields = _form_fields(ctx)
        if not urls: return None
        # на MVP шлём только по первой форме
        return {"url": urls[0], "fields": fields}

    def send(self, job: Dict[str, Any]) -> Dict[str, Any]:
        # Прямо здесь используем Playwright (без отдельного infra-хелпера)
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        url, fields = job["url"], job["fields"]
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)

                def try_fill(name: str, value: str):
                    # поиск по name
                    sel = f'input[name="{name}"], textarea[name="{name}"]'
                    if page.locator(sel).count() > 0:
                        page.fill(sel, value); return True
                    # по placeholder
                    inputs = page.locator("input, textarea")
                    n = inputs.count()
                    for i in range(n):
                        ph = (inputs.nth(i).get_attribute("placeholder") or "")
                        if re.search(name, ph, re.I):
                            inputs.nth(i).fill(value); return True
                    # по label
                    labels = page.locator("label")
                    ln = labels.count()
                    for i in range(ln):
                        text = (labels.nth(i).inner_text() or "").strip()
                        if re.search(name, text, re.I):
                            for_id = labels.nth(i).get_attribute("for")
                            if for_id:
                                page.fill(f"#{for_id}", value); return True
                    return False

                for k, v in fields.items():
                    try_fill(k, v)

                # нажмём кнопку
                clicked = False
                for label in ["Send", "Submit", "Отправить", "Send message", "Отправка"]:
                    btn = page.get_by_role("button", name=label)
                    if btn.count() > 0:
                        btn.first.click(); clicked = True; break
                if not clicked:
                    # fallback: первый type=submit
                    subs = page.locator('button[type="submit"], input[type="submit"]')
                    if subs.count() > 0:
                        subs.first.click(); clicked = True

                # подождём подтверждение/редирект/спиннер
                time.sleep(1.2)
                browser.close()
                return {"ok": True, "meta": {"url": url}}
        except PWTimeout:
            log.error("Form submit timeout: %s", url)
            return {"ok": False, "meta": {"error": "timeout"}}
        except Exception as e:
            log.exception("Form submit error: %s", e)
            return {"ok": False, "meta": {"error": str(e)}}
