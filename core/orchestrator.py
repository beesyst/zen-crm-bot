from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Optional

import yaml
from app.adapters.crm.kommo import KommoAdapter
from domain.services.enrich import enrich_company_by_url
from domain.services.seed import seed_company_from_url

# Терминальные метки
from core.console import add, error, finish, ok, skip, start, update

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

    if not (
        settings.get("modes", {}).get("research_and_intake", {}).get("enabled", False)
    ):
        skip("research", "disabled in settings.yml")
        _log.info("[skip]  research (disabled in settings.yml)")
        return

    sites = _load_sites()
    start()
    if not sites:
        skip("research", "config/sites.yml is empty")
        _log.info("[skip]  research (config/sites.yml is empty)")
        finish()
        return

    sites = list(_take_limit(sites, opts.limit))
    ok(f"research: {len(sites)} sites")
    _log.info("[ok]     research: %s sites", len(sites))

    # прокинем settings
    crm = KommoAdapter()

    for url in sites:
        app = brand_from_url(url) or "project"
        _log.info("Создание main.json - %s - %s", app, url)
        _log.info("Сбор соц линков - %s - %s", app, url)

        t0 = time.time()
        try:
            if opts.dry_run:
                _ = seed_company_from_url(crm, url, settings)
                secs = int(time.time() - t0)
                skip(url, f"dry-run ({secs}s)")
                _log.info("[skip]   %s - %s (dry-run, %ss)", app, url, secs)
            else:
                created = seed_company_from_url(crm, url, settings)
                secs = int(time.time() - t0)
                if created:
                    add(url, secs)
                    _log.info("[add]    %s - %s - %s sec", app, url, secs)
                else:
                    skip(url, "already exists")
                    _log.info("[skip]   %s - %s (already exists)", app, url)
        except Exception as e:
            error(url, str(e))
            _log.error("[error]  %s - %s - %s", app, url, e)
            if opts.stop_on_error:
                break

        if opts.rate_limit_sec > 0:
            time.sleep(opts.rate_limit_sec)

    finish()
    _log.info("Готово (research)")


# Пайплайн 2: Enrich Existing
def run_enrich_pipeline(options: OrchestratorOptions | None = None) -> None:
    opts = options or OrchestratorOptions()
    settings = _load_settings()

    if not settings.get("modes", {}).get("enrich_existing", {}).get("enabled", False):
        skip("enrich", "disabled in settings.yml")
        _log.info("[skip]  enrich (disabled in settings.yml)")
        return

    # передаем settings в адаптер, чтобы работали safe_mode/fields и т.д.
    crm = KommoAdapter()
    tags = settings["modes"]["enrich_existing"].get("tag_process", ["new"])
    companies = crm.find_companies_by_tags(tags)

    start()
    if not companies:
        skip("enrich", f"no companies with tags {','.join(tags)}")
        _log.info("[skip]  enrich (no companies with tags %s)", ",".join(tags))
        finish()
        return

    companies = list(_take_limit(companies, opts.limit))
    ok(f"enrich: {len(companies)} companies")
    _log.info("[ok]     enrich: %s companies", len(companies))

    for c in companies:
        url = crm.get_company_web(c)
        cid = c.get("id")

        if not url:
            skip(f"company:{cid}", "no web")
            _log.info("[skip]   company:%s (no web)", cid)
            continue

        app = (c.get("name") or "").strip() or brand_from_url(url) or "project"
        _log.info("Создание main.json - %s - %s", app, url)
        _log.info("Сбор соц линков - %s - %s", app, url)

        t0 = time.time()
        try:
            if opts.dry_run:
                # проверяем, что пайплайн отрабатывает без реального апдейта
                _ = enrich_company_by_url(crm, c, url, settings)
                secs = int(time.time() - t0)
                skip(url, f"dry-run ({secs}s)")
                _log.info("[skip]   %s - %s (dry-run, %ss)", app, url, secs)
            else:
                changed = enrich_company_by_url(crm, c, url, settings)
                secs = int(time.time() - t0)
                if changed:
                    update(url, secs)
                    _log.info("[update] %s - %s - %s sec", app, url, secs)
                else:
                    skip(url, "no changes")
                    _log.info("[skip]   %s - %s (no changes)", app, url)
        except Exception as e:
            error(url, str(e))
            _log.error("[error]  %s - %s - %s", app, url, e)
            if opts.stop_on_error:
                break

        if opts.rate_limit_sec > 0:
            time.sleep(opts.rate_limit_sec)

    finish()
    _log.info("Готово (enrich)")


# Комбайнер по настройкам
def run_enabled_pipelines(options: OrchestratorOptions | None = None) -> None:
    settings = _load_settings()

    if settings.get("modes", {}).get("research_and_intake", {}).get("enabled", False):
        run_research_pipeline(options)

    if settings.get("modes", {}).get("enrich_existing", {}).get("enabled", False):
        run_enrich_pipeline(options)
