from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml
from core.log_setup import get_logger, get_request_id, request_id_var, setup_logging
from core.paths import ensure_dirs
from fastapi import FastAPI, Request

# Подготовка папок logs/storage
ensure_dirs()

# Единая настройка логирования (без .env)
setup_logging(level="INFO", service="zen-crm", env="dev", write_files=True)
log = get_logger("host")


# Ленивая загрузка конфигурации YAML по SETTINGS_PATH (или config/settings.yml)
def _load_settings() -> Dict[str, Any]:
    settings_path = os.getenv("SETTINGS_PATH", "config/settings.yml")
    path = Path(settings_path)

    if not path.exists():
        log.error("settings.yml не найден", extra={"settings_path": settings_path})
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # лог только верхний уровень ключей (без значений)
        log.info(
            "settings.yml загружен",
            extra={"settings_path": str(path), "top_level_keys": list(data.keys())},
        )
        return data
    except Exception:
        log.exception("Ошибка парсинга settings.yml")
        return {}


# Инициализация FastAPI и сохранение настроек в app.state
app = FastAPI(title="Outreach Automator API")
app.state.settings = _load_settings()

# Подключение маршрутов: вебхуки Kommo и админ-ручки
try:
    from app.routes import admin as admin_routes
    from app.routes import webhooks as webhooks_routes

    app.include_router(webhooks_routes.router)
    app.include_router(admin_routes.router)
    log.info("routers registered", extra={"routers": ["webhooks", "admin"]})
except Exception:
    log.exception("routers registration failed")


# Мидлвар: пробрасываем X-Request-ID в контекст логов и в ответ
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or get_request_id()
    request_id_var.set(rid)
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


# Хук старта: короткий sanity-check загруженных секций
@app.on_event("startup")
async def on_startup():
    s = app.state.settings if isinstance(app.state.settings, dict) else {}
    log.info(
        "API startup",
        extra={
            "settings_loaded": bool(s),
            "have_infra": "infra" in s,
            "have_crm": "crm" in s,
            "have_mail": "mail" in s,
        },
    )


# Healthcheck без утечки конфиденциальной информации
@app.get("/health")
def health():
    return {"status": "ok"}
