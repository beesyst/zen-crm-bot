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

    sites = list(_take_limit(sites, opts.limit))
    processed_total = 0

    # подключаем CRM адаптер
    crm = KommoAdapter()

    for url in sites:
        app = brand_from_url(url) or "project"
        # технические шаги только в host.log
        _log.info("Создание main.json - %s - %s", app, url)
        _log.info("Сбор соц линков - %s - %s", app, url)

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

    # режим выключен - коротко в терминал, чисто в host.log
    if not settings.get("modes", {}).get("enrich_existing", {}).get("enabled", False):
        skip("enrich", "disabled in settings.yml")
        _log.info("enrich отключён в settings.yml")
        return

    crm = KommoAdapter()
    tags = settings["modes"]["enrich_existing"].get("tag_process", ["new"])
    companies = crm.find_companies_by_tags(tags)

    # старт пайплайна: только терминал
    ok("start enrich")

    if not companies:
        # ничего не обогащаем: терминал + финальная строка в host.log без префиксов
        ok("total: 0")
        _log.info("enrich: 0 companies")
        finish()
        return

    companies = list(_take_limit(companies, opts.limit))

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
        _log.info("Создание main.json - %s - %s", app, url)
        _log.info("Сбор соц линков - %s - %s", app, url)

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
