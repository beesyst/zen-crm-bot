from __future__ import annotations
from typing import Dict, Any, Optional
import logging, os, requests
from modules.base import OutreachChannel

log = logging.getLogger("outreach.telegram")
BASE = lambda: f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN','')}"

def _pick_chat_id(ctx: Dict[str, Any]) -> Optional[int]:
    # ожидаем chat_id прямо; username -> chat_id оставим на будущее (require user opt-in)
    cid = ctx.get("telegram_chat_id")
    try:
        return int(cid) if cid is not None else None
    except Exception:
        return None

def _default_text(ctx: Dict[str, Any]) -> str:
    proj = ctx.get("project_name") or ctx.get("name") or "проект"
    return f"Привет! По {proj} хотим обсудить коллаборацию. Откликнитесь, если интересно."

class TelegramChannel(OutreachChannel):
    kind = "telegram"

    def available(self, ctx: Dict[str, Any]) -> bool:
        return bool(os.getenv("TELEGRAM_BOT_TOKEN")) and _pick_chat_id(ctx) is not None

    def build_job(self, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        chat_id = _pick_chat_id(ctx)
        if not chat_id: return None
        text = ctx.get("telegram_text") or _default_text(ctx)
        return {"chat_id": chat_id, "text": text}

    def send(self, job: Dict[str, Any]) -> Dict[str, Any]:
        try:
            r = requests.post(f"{BASE()}/sendMessage",
                              json={"chat_id": job["chat_id"], "text": job["text"], "disable_web_page_preview": True},
                              timeout=20)
            ok = 200 <= r.status_code < 300 and r.json().get("ok")
            if not ok:
                log.error("Telegram send failed: %s %s", r.status_code, r.text[:300])
            return {"ok": ok, "meta": {"status": r.status_code}}
        except Exception as e:
            log.exception("Telegram send error: %s", e)
            return {"ok": False, "meta": {"error": str(e)}}
