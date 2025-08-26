from __future__ import annotations

from typing import Any, Dict

from app.adapters.crm.kommo import KommoAdapter
from core.settings import get_settings
from domain.services.ingest import ingest_from_website


def _field_ids() -> Dict[str, int]:
    return get_settings()["crm"]["kommo"]["fields"]


def _extract_site_from_lead(lead: Dict[str, Any], ids: Dict[str, int]) -> str | None:
    # пробуем взять из кастом-поля "Site"
    for cf in lead.get("custom_fields_values") or []:
        if cf.get("field_id") == ids.get("site"):
            vals = cf.get("values") or []
            if vals and vals[0].get("value"):
                return str(vals[0]["value"]).strip()
    # fallback: из названия/заметок не трогаем
    return None


def bootstrap_new_lead(lead_id: int) -> Dict[str, Any]:
    crm = KommoAdapter()
    ids = _field_ids()

    lead = crm.get_lead(lead_id)
    name = lead.get("name") or "Project"
    site = _extract_site_from_lead(lead, ids) or ""

    # инжест с сайта (если есть)
    enrich = {}
    if site:
        found = ingest_from_website(website=site, project_name=name) or {}
        # простые поля из инжеста
        if found.get("website"):
            enrich["site"] = found["website"]
        if found.get("docs"):
            enrich["docs"] = found["docs"]
        if found.get("info"):
            enrich["info"] = found["info"]
        if found.get("linkedin"):
            enrich["linkedin"] = found["linkedin"]
        if found.get("discord_webhook") or found.get("discord"):
            enrich["discord"] = found.get("discord_webhook") or found.get("discord")
        if found.get("telegram") or found.get("telegram_chat_id"):
            enrich["telegram"] = found.get("telegram") or found.get("telegram_chat_id")

    # простейшие эвристики Tier/DM
    if "tier" not in enrich:
        # пример: если есть LinkedIn и Docs - Tier=A (иначе B)
        enrich["tier"] = "A" if (enrich.get("linkedin") and enrich.get("docs")) else "B"
    if "dm" not in enrich:
        enrich["dm"] = "No"

    # готовим патч custom_fields_values по id
    patch = {}
    for key, val in enrich.items():
        fid = ids.get(key)
        if fid:
            patch[fid] = val

    if patch:
        crm.update_custom_fields(lead_id, patch)

    # заметка + перевод стадии (опц.)
    note = f"Auto-enriched: site={enrich.get('site')}, li={enrich.get('linkedin')}, dc={enrich.get('discord')}, tg={enrich.get('telegram')}, tier={enrich.get('tier')}"
    crm.add_note(lead_id, note)

    # сразу в работу: READY_FOR_OUTREACH
    crm.set_stage(lead_id, "READY_FOR_OUTREACH")

    return {"lead_id": lead_id, "enriched": enrich}
