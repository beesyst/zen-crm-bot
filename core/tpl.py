from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Dict

import yaml

from .log_setup import get_logger

# Локальные инструменты проекта
from .paths import PROJECT_ROOT as ROOT

LOG = get_logger("setup")

# Константы путей шаблонов/файлов
TEMPLATES_DIR = ROOT / "core" / "templates"
SETTINGS_PATH = ROOT / "config" / "settings.yml"

SETTINGS_EXAMPLE_TPL = TEMPLATES_DIR / "settings.example.yml.tpl"
SETTINGS_EXAMPLE_OUT = TEMPLATES_DIR / "settings.example.yml"

ENV_PATH = ROOT / ".env"

# Карта путей секретов
SECRET_PATHS: Dict[str, Any] = {
    "crm.kommo.access_token": "PASTE_TOKEN",
    "mail.smtp_pass": "secret",
    "channels.telegram.bot_token": "xxx",
}

# Регулярка для плейсхолдеров {{ dotted.path | default(...) }}
TOKEN_RE = re.compile(r"\{\{\s*([^\}\|]+?)(?:\s*\|\s*default\((.*?)\))?\s*\}\}")


# Чтение config/settings.yml как dict (или пустой dict)
def _load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            return yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
        except Exception as e:
            LOG.error("settings.yml parse error: %s", e)
            return {}
    return {}


# Возврат значения по dotted-пути (или None)
def _lookup(context: dict, dotted: str):
    cur = context
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


# Парс литерала по умолчанию в тип (YAML для простоты)
def _parse_default(literal: str):
    try:
        return yaml.safe_load(literal)
    except Exception:
        return literal


# Рендер текста шаблона, подставляя {{ dotted.path }} из контекста
def _render_text(tpl_text: str, context: dict) -> str:
    def repl(m: re.Match):
        dotted = m.group(1).strip()
        default_literal = m.group(2)
        val = _lookup(context, dotted)

        if val is None and default_literal is not None:
            # default(...) указан - парсим и подставляем
            return str(_parse_default(default_literal))
        if val is None:
            # нет значения и нет default(...) - подставляем пустую строку
            return ""

        # для сложных типов сериализуем одним словом (flow-style)
        if isinstance(val, (dict, list, bool, int, float)) and not isinstance(val, str):
            try:
                return yaml.safe_dump(val, default_flow_style=True).strip()
            except Exception:
                return str(val)
        return str(val)

    return TOKEN_RE.sub(repl, tpl_text)


# Копия настроек и редактирование секретов заглушками
def _redact_secrets(settings: dict) -> dict:
    s = copy.deepcopy(settings) or {}
    for path, placeholder in SECRET_PATHS.items():
        cur = s
        parts = path.split(".")
        for p in parts[:-1]:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                cur = None
                break
        if isinstance(cur, dict):
            cur[parts[-1]] = placeholder
    return s


# Обновление/добавление ключ=значение в .env (без дублей)
def _ensure_env_kv(env_path: Path, key: str, value: str) -> None:
    lines = (
        env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    )
    out = []
    found = False
    for ln in lines:
        if ln.strip().startswith(f"{key}="):
            found = True
            if value:
                out.append(f"{key}={value}")
            # если пустое значение - ключ удаляем
        else:
            out.append(ln)
    if not found and value:
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


# Взятие из settings.yml значения images.<name> (или пустую строку)
def _read_settings_image(name: str, default: str = "") -> str:
    settings = _load_settings()
    return (settings.get("images", {}) or {}).get(name, "") or default


# core/templates/settings.example.yml из settings.example.yml.tpl и config/settings.yml
def generate_settings_example() -> None:
    if not SETTINGS_EXAMPLE_TPL.exists():
        LOG.info("skip: %s not found", SETTINGS_EXAMPLE_TPL)
        return

    settings = _load_settings()
    redacted = _redact_secrets(settings)
    tpl_text = SETTINGS_EXAMPLE_TPL.read_text(encoding="utf-8")

    try:
        rendered = _render_text(tpl_text, redacted)
        SETTINGS_EXAMPLE_OUT.write_text(rendered, encoding="utf-8")
        LOG.info("rendered %s from %s", SETTINGS_EXAMPLE_OUT, SETTINGS_EXAMPLE_TPL)
    except Exception as e:
        LOG.error("render settings example failed: %s", e)


# Синхронизация .env с config/settings.yml для нужд docker-compose
def sync_env_from_settings() -> None:
    try:
        _ensure_env_kv(ENV_PATH, "SETTINGS_PATH", "config/settings.yml")
        _ensure_env_kv(ENV_PATH, "POSTGRES_IMAGE", _read_settings_image("postgres"))
        _ensure_env_kv(ENV_PATH, "REDIS_IMAGE", _read_settings_image("redis"))
        LOG.info(".env synced from settings.yml")
    except Exception as e:
        LOG.error(".env sync failed: %s", e)
