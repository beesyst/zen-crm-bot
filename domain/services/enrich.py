from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

from app.adapters.crm.kommo import KommoAdapter
from core.collector import collect_main_data
from core.log_setup import get_logger
from core.normalize import force_https, normalize_socials
from core.paths import MAIN_TEMPLATE, STORAGE_PROJECTS

_log = get_logger("orchestrator")


# JSON-файл
def _read_json(p: Path) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# Запись словаря в JSON с созданием директорий
def _write_json(p: Path, data: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# Превращение URL в "слаг" (корневой хост без www и порта)
def _slug(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc.split(":")[0]
    except Exception:
        return (url or "").strip().lower()


# Загрузка JSON-шаблона main_template.json (если нет - минимальный каркас)
def _load_template() -> Dict[str, Any]:
    try:
        return json.loads(MAIN_TEMPLATE.read_text(encoding="utf-8"))
    except Exception:
        return {"socialLinks": {}}


# Считывание текузих кастом полей компании Kommo (field_id -> value)
def _current_cf(company: Dict[str, Any]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for f in company.get("custom_fields_values") or company.get("custom_fields") or []:
        fid = f.get("field_id")
        vals = f.get("values") or []
        if fid is None or not vals:
            continue
        for v in vals:
            val = (v.get("value") or "").strip()
            if val:
                out[int(fid)] = val
                break
    return out


# Значение в поле (учитывая no_overwrite)
def _should_write(
    fid: int, val: str, current: Dict[int, str], no_overwrite: bool
) -> bool:
    if not val:
        return False
    return (
        (not no_overwrite) or (fid not in current) or (not current.get(fid, "").strip())
    )


# Формирование словаря обновлений кастомных полей Kommo по найденным соцссылкам
def _plan_updates(
    socials: Dict[str, str], settings: Dict[str, Any], company: Dict[str, Any]
) -> Dict[int, str]:
    fields_cfg = (settings.get("crm") or {}).get("kommo", {}).get("fields", {}) or {}
    no_overwrite = (
        (settings.get("crm") or {})
        .get("kommo", {})
        .get("safe_mode", {})
        .get("no_overwrite", True)
    )

    tw = socials.get("twitterURL") or ""
    if tw:
        tw = force_https(tw.replace("twitter.com", "x.com"))

    want = {
        "web": socials.get("websiteURL") or "",
        "docs": socials.get("documentURL") or "",
        "x": tw,
        "discord": socials.get("discordURL") or "",
        "github": socials.get("githubURL") or "",
        "linkedin": socials.get("linkedinURL") or "",
        "telegram": socials.get("telegramURL") or "",
        "reddit": socials.get("redditURL") or "",
        "youtube": socials.get("youtubeURL") or "",
        "medium": socials.get("mediumURL") or "",
    }

    current = _current_cf(company)
    updates: Dict[int, str] = {}
    for key, value in want.items():
        fid = fields_cfg.get(key)
        if not fid:
            continue
        try:
            fid_int = int(fid)
        except Exception:
            continue
        if _should_write(fid_int, value, current, no_overwrite):
            updates[fid_int] = value
    return updates


# Изменение JSON (без учета порядка ключей)
def _is_changed(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return json.dumps(a, sort_keys=True, ensure_ascii=False) != json.dumps(
        b, sort_keys=True, ensure_ascii=False
    )


# Главная функция: сбор main.json + обновление кастом полей компании в Kommo
def enrich_company_by_url(
    crm: KommoAdapter, company: Dict[str, Any], url: str, settings: Dict[str, Any]
) -> bool:
    slug = _slug(url)
    project_dir = STORAGE_PROJECTS / slug
    main_path = project_dir / "main.json"

    template = _load_template()
    data = collect_main_data(url, template, str(project_dir))

    prev = _read_json(main_path) if main_path.exists() else {}
    if prev.get("contacts"):
        data.setdefault("contacts", {})
        for key in ("emails", "forms", "persons"):
            if not data["contacts"].get(key):
                data["contacts"][key] = prev["contacts"].get(key, [])
    json_changed = _is_changed(prev, data)
    if json_changed or not main_path.exists():
        _write_json(main_path, data)
        _log.info("main.json сохранен: %s", str(main_path))

    socials_raw = (data or {}).get("socialLinks") or {}
    socials = normalize_socials(socials_raw)

    # приводим twitter → x
    tw = socials.get("twitterURL") or ""
    if tw:
        socials["twitterURL"] = force_https(tw.replace("twitter.com", "x.com"))

    updates = _plan_updates(socials, settings, company)

    kommo_changed = False
    if updates:
        dry_run = (
            (settings.get("crm") or {})
            .get("kommo", {})
            .get("safe_mode", {})
            .get("dry_run", False)
        )
        if not dry_run:
            crm.update_company_custom_fields(int(company["id"]), updates)
        kommo_changed = True

    return bool(json_changed or kommo_changed)
