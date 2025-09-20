from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

import yaml

from core.normalize import (
    force_https,
    normalize_host_list,
)

_BASE = Path(__file__).resolve().parents[1]


def _settings_path() -> Path:
    env_rel = os.getenv("SETTINGS_PATH")
    return (_BASE / env_rel) if env_rel else (_BASE / "config" / "settings.yml")


_cache: Dict[str, Any] | None = None


# Сброс кэша конфигурации
def reset_settings_cache() -> None:
    global _cache
    _cache = None


def get_settings() -> Dict[str, Any]:
    global _cache
    if _cache is None:
        path = _settings_path()
        with open(path, "r", encoding="utf-8") as f:
            _cache = yaml.safe_load(f) or {}
    return _cache


def get_flag(name: str, default: bool = False) -> bool:
    return bool(get_settings().get(name, default))


def get_image(name: str) -> str:
    images = get_settings().get("images") or {}
    val = images.get(name)
    if not val:
        raise RuntimeError(f"config/settings.yml: images.{name} обязателен")
    return val


# Возврат списка ключей соц-ссылок
def get_social_keys() -> list[str]:
    raw = (get_settings().get("socials") or {}).get("keys") or []
    out: List[str] = []
    seen = set()
    for k in raw:
        if isinstance(k, str):
            kk = k.strip()
            if kk and kk not in seen:
                out.append(kk)
                seen.add(kk)
    return out


# Возврат нормализованных доменов соцсетей из YAML
def get_social_hosts() -> list[str]:
    raw = (get_settings().get("socials") or {}).get("social_hosts") or []
    return normalize_host_list(raw)


# Возврат нормализованных доменов линк-агрегаторов из YAML
def get_link_collections() -> list[str]:
    return normalize_host_list(get_settings().get("link_collections") or [])


# Возврат конфига Nitter
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


# Словарь ролей контактов из settings.yml (contacts.roles)
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
