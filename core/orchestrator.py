from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Optional

import yaml
from app.adapters.crm.kommo import KommoAdapter
from domain.services.enrich import enrich_company_by_url
from domain.services.seed import seed_company_from_url

# Терминальные метки
from core.console import add, error, finish, ok, skip, update

# Единый файловый лог host.log
from core.log_setup import get_logger

# Нормализованное имя/токен из URL для логов
from core.normalize import brand_from_url

# Путь к конфигам
from core.paths import CONFIG_DIR

# Логгер, который пишет в logs/host.log с меткой [orchestrator]
_log = get_logger("orchestrator")


# Конфигурация исполнения пайплайнов
@dataclass
class OrchestratorOptions:
    limit: Optional[int] = None
    dry_run: bool = False
    stop_on_error: bool = False
    rate_limit_sec: float = 0.0


# Чтение config/settings.yml и возвращение словаря настроек
def _load_settings() -> dict:
    p = CONFIG_DIR / "settings.yml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}


# Чтение config/sites.yml → список сайтов для режима research
def _load_sites() -> list[str]:
    sites_yaml = CONFIG_DIR / "sites.yml"
    if not sites_yaml.exists():
        return []
    data = yaml.safe_load(sites_yaml.read_text(encoding="utf-8")) or {}
    sites = data.get("sites", [])
    return [s for s in sites if isinstance(s, str) and s.strip()]


# Применение ограничения количества элементов (если limit задан)
def _take_limit(items: Iterable, limit: Optional[int]) -> Iterable:
    if limit is None or limit <= 0:
        return items
    lst = list(items)
    return lst[:limit]


# Пайплайн 1: Research & Intake
def run_research_pipeline(options: OrchestratorOptions | None = None) -> None:
    opts = options or OrchestratorOptions()
    settings = _load_settings()

    # режим выключен - коротко в терминал, чисто в host.log
    if not (
        settings.get("modes", {}).get("research_and_intake", {}).get("enabled", False)
    ):
        skip("research", "disabled in settings.yml")
        _log.info("research отключён в settings.yml")
        return

    sites = _load_sites()

    # старт пайплайна: явный режим в терминале
    ok("start research")

    if not sites:
        # нет источников - фиксируем итог и выходим
        ok("total: 0")
        _log.info("research: 0 sites")
        finish()
        return

    mode_cfg = settings.get("modes", {}).get("research_and_intake", {}) or {}
    mode_limit = int(mode_cfg.get("limit")) if mode_cfg.get("limit") else None
    rate_limit_cfg = float(mode_cfg.get("rate_limit_sec") or 0)

    # приоритет: CLI-опции > конфиг
    effective_limit = opts.limit if (opts.limit and opts.limit > 0) else mode_limit
    if opts.rate_limit_sec <= 0 and rate_limit_cfg > 0:
        opts.rate_limit_sec = rate_limit_cfg

    sites = list(_take_limit(sites, effective_limit))
    processed_total = 0

    # подключаем CRM адаптер
    crm = KommoAdapter()

    for url in sites:
        app = brand_from_url(url) or "project"

        t0 = time.time()
        try:
            if opts.dry_run:
                _ = seed_company_from_url(crm, url, settings)
                secs = int(time.time() - t0)
                # терминал
                skip(url, f"dry-run ({secs}s)")
                # host.log
                _log.info("Пропуск %s (dry-run, %ss)", url, secs)
            else:
                created = seed_company_from_url(crm, url, settings)
                secs = int(time.time() - t0)
                if created:
                    processed_total += 1
                    # терминал
                    add(url, secs)
                    # host.log
                    _log.info("Добавлено %s - %s - %s sec", app, url, secs)
                else:
                    # терминал
                    skip(url, "already exists")
                    # host.log
                    _log.info("Пропуск %s (уже есть)", url)
        except Exception as e:
            # терминал (идёт через console -> спиннеры там)
            error(url, str(e))
            # host.log с трейсбеком
            _log.exception("Ошибка %s - %s", app, url)
            if opts.stop_on_error:
                break

        if opts.rate_limit_sec > 0:
            time.sleep(opts.rate_limit_sec)

    # финальная сводка
    ok(f"total: {processed_total}")
    _log.info("research: %s sites", processed_total)
    finish()


# Пайплайн 2: Enrich Existing
def run_enrich_pipeline(options: OrchestratorOptions | None = None) -> None:
    opts = options or OrchestratorOptions()
    settings = _load_settings()

    if not settings.get("modes", {}).get("enrich_existing", {}).get("enabled", False):
        skip("enrich", "disabled in settings.yml")
        _log.info("enrich отключён в settings.yml")
        return

    mode_cfg = settings.get("modes", {}).get("enrich_existing", {}) or {}
    tag_ids = list(mode_cfg.get("tag_id") or [])
    tag_names = list(mode_cfg.get("tag_process") or [])

    # конфигурируемые параметры режима
    page_size = int(mode_cfg.get("page_size") or 250)
    mode_limit = int(mode_cfg.get("limit")) if mode_cfg.get("limit") else None
    rate_limit_cfg = float(mode_cfg.get("rate_limit_sec") or 0)

    # приоритет: CLI-опции > конфиг
    effective_limit = opts.limit if (opts.limit and opts.limit > 0) else mode_limit
    if opts.rate_limit_sec <= 0 and rate_limit_cfg > 0:
        opts.rate_limit_sec = rate_limit_cfg

    # инициализируем адаптер
    crm = KommoAdapter()

    # если tag_id пуст - резолвим по именам из tag_process
    if not tag_ids and tag_names:
        tag_ids = crm.resolve_tag_ids(tag_names)

    # если и после резолва пусто - корректно выходим
    if not tag_ids:
        skip("enrich", "нет tag_id (проверь modes.enrich_existing.tag_id/tag_process)")
        _log.info("enrich: пустой список tag_id")
        ok("total: 0")
        finish()
        return

    ok("start enrich")

    companies = list(crm.iter_companies_by_tag_ids(tag_ids, limit=page_size))

    if not companies:
        ok("total: 0")
        _log.info("enrich: 0 companies")
        finish()
        return

    # ограничение итогового набора по effective_limit (если задан)
    companies = list(_take_limit(companies, effective_limit))

    processed_total = 0

    for c in companies:
        url = crm.get_company_web(c)
        cid = c.get("id")

        if not url:
            # терминал
            skip(f"company:{cid}", "no web")
            # host.log
            _log.info("Пропуск company:%s (нет сайта)", cid)
            continue

        app = (c.get("name") or "").strip() or brand_from_url(url) or "project"

        # технические шаги в host.log
        _log.info("Старт %s - %s", app, url)

        t0 = time.time()
        try:
            if opts.dry_run:
                _ = enrich_company_by_url(crm, c, url, settings)
                secs = int(time.time() - t0)
                # терминал
                skip(url, f"dry-run ({secs}s)")
                # host.log — без приставок
                _log.info("Пропуск %s (dry-run, %ss)", url, secs)
            else:
                changed = enrich_company_by_url(crm, c, url, settings)
                secs = int(time.time() - t0)
                if changed:
                    processed_total += 1
                    # терминал
                    update(url, secs)
                    # host.log — без приставок
                    _log.info("Обновлено %s - %s - %s sec", app, url, secs)
                else:
                    # терминал
                    skip(url, "no changes")
                    # host.log — без приставок
                    _log.info("Пропуск %s (нет изменений)", url)
        except Exception as e:
            error(url, str(e))
            _log.error("Ошибка %s - %s - %s", app, url, e)
            if opts.stop_on_error:
                break

        if opts.rate_limit_sec > 0:
            time.sleep(opts.rate_limit_sec)

    # финальная сводка: терминал + чистая строка в host.log
    ok(f"total: {processed_total}")
    _log.info("enrich: %s companies", processed_total)
    finish()


# Комбайнер по настройкам
def run_enabled_pipelines(options: OrchestratorOptions | None = None) -> None:
    settings = _load_settings()

    if settings.get("modes", {}).get("research_and_intake", {}).get("enabled", False):
        run_research_pipeline(options)

    if settings.get("modes", {}).get("enrich_existing", {}).get("enabled", False):
        run_enrich_pipeline(options)
