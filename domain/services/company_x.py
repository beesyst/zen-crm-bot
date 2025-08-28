from __future__ import annotations

from typing import Any, Dict, Optional

from app.adapters.crm.kommo import KommoAdapter
from core.settings import get_settings


# ID кастомных полей компании из settings.yml
def _fields() -> Dict[str, int]:
    return get_settings().get("crm", {}).get("kommo", {}).get("fields", {}) or {}


# Safe-mode настройки (dry_run, no_overwrite)
def _safe() -> Dict[str, Any]:
    return get_settings().get("crm", {}).get("kommo", {}).get("safe_mode", {}) or {}


# Режимы работы бота (research, enrich)
def _modes() -> Dict[str, Any]:
    return get_settings().get("modes", {}).get("enrich_existing", {}) or {}


# Значение кастомного поля компании по field_id
def _get_cf(company: Dict[str, Any], field_id: int) -> Optional[str]:
    for cf in company.get("custom_fields_values") or []:
        if int(cf.get("field_id", 0)) == int(field_id):
            vals = cf.get("values") or []
            if vals and vals[0].get("value"):
                return str(vals[0]["value"]).strip()
    return None


# Безопасное проставление X (Twitter) в кастом-поле компании по id
def set_company_x(company_id: int, x_url: str) -> Dict[str, Any]:
    cfg_fields = _fields()
    safe = _safe()
    modes = _modes()

    x_id = cfg_fields.get("x")
    assert x_id, "config.crm.kommo.fields.x must be set to field_id"

    no_overwrite = bool(safe.get("no_overwrite", True))
    dry_run = bool(safe.get("dry_run", False))
    only_if_has_any_tag = set(modes.get("tag_process") or [])

    crm = KommoAdapter()
    company = crm.get_company(company_id)

    # фильтр по тегам
    if only_if_has_any_tag:
        tags = [
            t.get("name", "") for t in (company.get("_embedded", {}).get("tags") or [])
        ]
        if not any(t in only_if_has_any_tag for t in tags):
            crm.add_company_note(
                company_id,
                f"Skip set X: tags {tags} do not match {list(only_if_has_any_tag)}",
            )
            return {"ok": False, "reason": "tag_filter", "tags": tags}

    # проверка на перезапись
    current = _get_cf(company, x_id)
    if current and no_overwrite:
        return {"ok": False, "reason": "exists", "current": current}

    # dry-run режим: только заметка
    if dry_run:
        crm.add_company_note(company_id, f"[DRY] X ← {x_url}")
        return {"ok": True, "patched": False, "dry_run": True}

    # обновление поля
    crm.update_company_custom_fields(company_id, {x_id: x_url})
    crm.add_company_note(company_id, f"X обновлён: {x_url}")
    return {"ok": True, "patched": True, "value": x_url}
