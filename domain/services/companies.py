from __future__ import annotations

from typing import Any, Dict

from app.adapters.crm.kommo import KommoAdapter
from core.settings import get_settings
from domain.services.ingest import ingest_from_website


# Возврат карты карту id кастомных полей компании из settings.yml
def _company_field_ids() -> Dict[str, int]:
    return (
        get_settings().get("crm", {}).get("kommo", {}).get("company_fields", {}) or {}
    )


def _extract_website(company: Dict[str, Any], ids: Dict[str, int]) -> str | None:
    for cf in company.get("custom_fields_values") or []:
        if ids.get("site") and int(cf.get("field_id", 0)) == int(ids["site"]):
            vals = cf.get("values") or []
            if vals and vals[0].get("value"):
                return str(vals[0]["value"]).strip()
        # подстрахуемся: некоторые аккаунты используют field_code
        code = str(cf.get("field_code") or "").lower()
        if code in {"website", "web"}:
            vals = cf.get("values") or []
            if vals and vals[0].get("value"):
                return str(vals[0]["value"]).strip()
    return None


# Обогащение компании по веб-сайту
def enrich_company(company_id: int) -> Dict[str, Any]:
    crm = KommoAdapter()
    ids = _company_field_ids()

    company = crm.get_company(company_id)
    name = company.get("name") or "Project"

    website = _extract_website(company, ids) or ""
    if not website:
        crm.add_company_note(company_id, "Auto-enrich: нет Web, пропуск.")
        return {"company_id": company_id, "ok": False, "reason": "no_website"}

    found = ingest_from_website(website=website, project_name=name) or {}

    # подготовим патч только по тем кастомным полям, id которых нам известны
    patch: Dict[int, Any] = {}
    if ids.get("site") and found.get("website"):
        patch[ids["site"]] = found["website"]
    if ids.get("docs") and found.get("docs"):
        patch[ids["docs"]] = found["docs"]
    if ids.get("info") and found.get("info"):
        patch[ids["info"]] = found["info"]
    if ids.get("linkedin") and found.get("linkedin"):
        patch[ids["linkedin"]] = found["linkedin"]
    if ids.get("discord") and (found.get("discord_webhook") or found.get("discord")):
        patch[ids["discord"]] = found.get("discord_webhook") or found.get("discord")
    if ids.get("telegram") and (found.get("telegram") or found.get("telegram_chat_id")):
        patch[ids["telegram"]] = found.get("telegram") or found.get("telegram_chat_id")

    if patch:
        crm.update_company_custom_fields(company_id, patch)

    note = (
        f"Auto-enriched company: site={found.get('website')}, "
        f"li={found.get('linkedin')}, "
        f"dc={found.get('discord') or found.get('discord_webhook')}, "
        f"tg={found.get('telegram') or found.get('telegram_chat_id')}"
    )
    crm.add_company_note(company_id, note)

    return {
        "company_id": company_id,
        "ok": True,
        "patched": bool(patch),
        "website": website,
    }
