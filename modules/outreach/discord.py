from __future__ import annotations
from typing import Dict, Any, Optional
import logging, re, requests, os
from modules.base import OutreachChannel

log = logging.getLogger("outreach.discord")

def _pick_webhook(ctx: Dict[str, Any]) -> Optional[str]:
    # 1) из лида/контекста
    wh = ctx.get("discord_webhook") or ctx.get("discord_webhook_url")
    # 2) из env как общий webhook (на случай теста)
    wh = wh or os.getenv("DISCORD_WEBHOOK_URL")
    if not wh: return None
    if not re.match(r"^https://discord\.com/api/webhooks/", wh):
        return None
    return wh

def _default_text(ctx: Dict[str, Any]) -> str:
    proj = ctx.get("project_name") or ctx.get("name") or "проект"
    return f"Привет! Изучили **{proj}** — есть идея синергии. Если интересно, дайте знать!"

class DiscordChannel(OutreachChannel):
    kind = "discord"

    def available(self, ctx: Dict[str, Any]) -> bool:
        return _pick_webhook(ctx) is not None

    def build_job(self, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        webhook = _pick_webhook(ctx)
        if not webhook: return None
        text = ctx.get("discord_text") or _default_text(ctx)
        return {"webhook": webhook, "content": text}

    def send(self, job: Dict[str, Any]) -> Dict[str, Any]:
        try:
            r = requests.post(job["webhook"], json={"content": job["content"]}, timeout=20)
            ok = 200 <= r.status_code < 300
            if not ok:
                log.error("Discord webhook failed: %s %s", r.status_code, r.text[:300])
            return {"ok": ok, "meta": {"status": r.status_code}}
        except Exception as e:
            log.exception("Discord send error: %s", e)
            return {"ok": False, "meta": {"error": str(e)}}
