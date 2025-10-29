from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Any, Dict, List

import yaml

from core.normalize import (
    force_https,
    normalize_host_list,
)

_BASE = Path(__file__).resolve().parents[1]


# Определяем путь к файлу настроек (можно переопределить через переменную окружения SETTINGS_PATH)
def _settings_path() -> Path:
    env_rel = os.getenv("SETTINGS_PATH")
    return (_BASE / env_rel) if env_rel else (_BASE / "config" / "settings.yml")


_cache: Dict[str, Any] | None = None


# Сброс кэша конфигурации
def reset_settings_cache() -> None:
    global _cache
    _cache = None


# Загрузка и возврат всех настроек (с кешированием)
def get_settings() -> Dict[str, Any]:
    global _cache
    if _cache is None:
        path = _settings_path()
        with open(path, "r", encoding="utf-8") as f:
            _cache = yaml.safe_load(f) or {}
    return _cache


# Получить булевый флаг из настроек
def get_flag(name: str, default: bool = False) -> bool:
    return bool(get_settings().get(name, default))


# Получить путь/имя картинки из блока images
def get_image(name: str) -> str:
    images = get_settings().get("images") or {}
    val = images.get(name)
    if not val:
        raise RuntimeError(f"config/settings.yml: images.{name} обязателен")
    return val


# Возвращает список ключей соц-сетей (socials.keys) с проверкой
def get_social_keys() -> list[str]:
    raw = (get_settings().get("socials") or {}).get("keys")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(
            "config/settings.yml: socials.keys обязателен и не может быть пустым"
        )
    out: List[str] = []
    seen = set()
    for k in raw:
        if not isinstance(k, str):
            raise RuntimeError(
                "config/settings.yml: socials.keys должен содержать строки"
            )
        kk = k.strip()
        if not kk:
            raise RuntimeError(
                "config/settings.yml: socials.keys содержит пустой элемент"
            )
        if kk not in seen:
            out.append(kk)
            seen.add(kk)
    return out


# Возвращает маппинг host → ключ соцсети из нового блока socials.host_map
def get_social_host_map() -> Dict[str, str]:
    conf = get_settings().get("socials") or {}
    host_map = conf.get("host_map")
    if not isinstance(host_map, dict) or not host_map:
        raise RuntimeError(
            "config/settings.yml: socials.host_map обязателен и не может быть пустым"
        )

    out: Dict[str, str] = {}
    for k, v in host_map.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise RuntimeError(
                "config/settings.yml: socials.host_map должен быть словарём строк → строк"
            )
        kk = k.strip().lower().replace("www.", "")
        vv = v.strip()
        if not kk or not vv:
            raise RuntimeError(
                "config/settings.yml: socials.host_map содержит пустые ключи/значения"
            )
        out[kk] = vv
    return out


# Возвращает нормализованный список доменов линк-агрегаторов
def get_link_collections() -> list[str]:
    return normalize_host_list(get_settings().get("link_collections") or [])


# Возвращает конфиг блока parser.nitter (валидирует и нормализует)
def get_nitter_cfg() -> dict:
    n = ((get_settings().get("parser") or {}).get("nitter")) or {}

    inst_in = n.get("instances") or []
    inst_out: List[str] = []
    seen = set()
    for x in inst_in:
        if not isinstance(x, str) or not x.strip():
            continue
        s = force_https(x.strip()).rstrip("/")
        if s and s not in seen:
            inst_out.append(s)
            seen.add(s)

    out = dict(n)
    out["enabled"] = bool(n.get("enabled", False))
    out["instances"] = inst_out
    if "retry_per_instance" in n:
        out["retry_per_instance"] = int(n.get("retry_per_instance"))
    if "timeout_sec" in n:
        out["timeout_sec"] = int(n.get("timeout_sec"))
    if "bad_ttl_sec" in n:
        out["bad_ttl_sec"] = int(n.get("bad_ttl_sec"))
    return out


# Нормализатор конфига LinkedIn
def get_linkedin_cfg() -> Dict[str, Any]:
    s = get_settings() or {}
    parser = s.get("parser") or {}
    li = parser.get("linkedin") or {}

    # базовый флаг
    enabled = bool(li.get("enabled", False))

    # аккаунт (только для полей)
    account = li.get("account") or {}
    out_account = {
        "email": str(account.get("email") or "").strip(),
        "useragent": str(account.get("useragent") or "").strip(),
        "cookies_base64": str(account.get("cookies_base64") or "").strip(),
        "totp_secret": str(account.get("totp_secret") or "").strip(),
    }

    # профили/файлы сессий (опционально, на будущее)
    persistent_profile = str(li.get("persistent_profile") or "").strip()
    cookies_path = str(li.get("cookies_path") or "").strip()

    # лимиты/скролл
    max_profiles = int(li.get("max_profiles") or 25)
    scroll_pages = int(li.get("scroll_pages") or 2)

    # роли-фильтры
    role_filters = []
    for x in li.get("role_filters") or []:
        if isinstance(x, str) and x.strip():
            role_filters.append(x.strip().lower())

    # троттлинг
    thrott = li.get("throttle")
    if not isinstance(thrott, dict):
        raise RuntimeError("config/settings.yml: parser.linkedin.throttle обязателен")

    try:
        throttle = {
            "min_action": int(thrott["min_action"]),
            "max_action": int(thrott["max_action"]),
            "burst_actions": int(thrott["burst_actions"]),
            "cool_down": int(thrott["cool_down"]),
        }
    except KeyError as e:
        raise RuntimeError(
            f"config/settings.yml: отсутствует ключ throttle.{e.args[0]}"
        )

    return {
        "enabled": enabled,
        "account": out_account,
        "persistent_profile": persistent_profile,
        "cookies_path": cookies_path,
        "max_profiles": max_profiles,
        "scroll_pages": scroll_pages,
        "role_filters": role_filters,
        "throttle": throttle,
    }


# Возвращает словарь ролей контактов (contacts.roles) с токенами
def get_contact_roles() -> Dict[str, list[str]]:
    roles = ((get_settings().get("contacts") or {}).get("roles")) or {}
    out: Dict[str, list[str]] = {}
    for role, tokens in roles.items() if isinstance(roles, dict) else []:
        if not isinstance(role, str) or not role.strip():
            continue
        seen = set()
        lst: list[str] = []
        for t in tokens or []:
            if not isinstance(t, str):
                continue
            tt = t.strip().lower()
            if tt and tt not in seen:
                lst.append(tt)
                seen.add(tt)
        if lst:
            out[role.strip().lower()] = lst
    return out


# Модульный счетчик для round_robin
_UA_RR_IDX: int = -1


# Единый User-Agent для всех HTTP/Playwright вызовов (жестко из конфига)
def get_http_ua() -> str:
    cfg = get_settings() or {}
    http = (cfg.get("parser") or {}).get("http") or {}

    strategy = http.get("strategy")
    ua_conf = http.get("ua")

    # строго валидируем стратегию
    if strategy not in ("single", "round_robin", "random"):
        raise RuntimeError(
            "config/settings.yml: parser.http.strategy должен быть 'single' | 'round_robin' | 'random'"
        )

    # single: должна быть строка
    if strategy == "single":
        if not isinstance(ua_conf, str) or not ua_conf.strip():
            raise RuntimeError(
                "config/settings.yml: parser.http.ua (string) обязателен для strategy=single"
            )
        return ua_conf.strip()

    # round_robin / random: должен быть непустой список строк
    if not isinstance(ua_conf, list) or not ua_conf:
        raise RuntimeError(
            "config/settings.yml: parser.http.ua (list) обязателен для strategy=round_robin|random"
        )
    ua_list = [str(x).strip() for x in ua_conf if isinstance(x, str) and x.strip()]
    if not ua_list:
        raise RuntimeError(
            "config/settings.yml: parser.http.ua (list) пуст после нормализации"
        )

    global _UA_RR_IDX
    if strategy == "round_robin":
        _UA_RR_IDX = (_UA_RR_IDX + 1) % len(ua_list)
        return ua_list[_UA_RR_IDX]

    # strategy == "random"
    return random.choice(ua_list)
