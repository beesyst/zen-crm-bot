from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml

_BASE = Path(__file__).resolve().parents[1]


def _settings_path() -> Path:
    env_rel = os.getenv("SETTINGS_PATH")
    return (_BASE / env_rel) if env_rel else (_BASE / "config" / "settings.yml")


_cache: Dict[str, Any] | None = None


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
