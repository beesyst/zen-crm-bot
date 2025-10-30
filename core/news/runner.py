from __future__ import annotations

import importlib
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

import yaml
from core.log_setup import get_logger
from core.normalize import _strip_tracking_params, normalize_url
from core.paths import CONFIG_DIR, PROJECT_ROOT
from core.storage import save_news_item

log = get_logger("news")


# Конфиг-структура для режима news_aggregator (частично дублирует settings.yml)
@dataclass
class NewsModeConfig:
    enabled: bool = False
    dry_run: bool = True
    projects_dir: Path = PROJECT_ROOT / "config" / "apps"
    storage_dir: Path = PROJECT_ROOT / "storage" / "news"
    backoff_base: int = 5
    backoff_max: int = 300
    schedule_slack: int = 300
    schedule_twitter: int = 600
    schedule_rss: int = 600


# Загрузка config/settings.yml
def _load_settings() -> dict:
    p = CONFIG_DIR / "settings.yml"
    if not p.exists():
        log.warning("settings.yml not found at %s", p)
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        log.exception("settings.yml parse error at %s", p)
        return {}


# Разбор modes.news_aggregator в NewsModeConfig
def _parse_news_mode(settings: dict) -> NewsModeConfig:
    md = (settings.get("modes") or {}).get("news_aggregator") or {}
    sched = md.get("schedule") or {}
    back = md.get("backoff") or {}
    return NewsModeConfig(
        enabled=bool(md.get("enabled", False)),
        dry_run=bool(md.get("dry_run", True)),
        projects_dir=Path(md.get("projects_dir") or (PROJECT_ROOT / "config" / "apps")),
        storage_dir=Path(md.get("storage_dir") or (PROJECT_ROOT / "storage" / "news")),
        backoff_base=int(back.get("base", 5)),
        backoff_max=int(back.get("max", 300)),
        schedule_slack=int(sched.get("slack_pull", 300)),
        schedule_twitter=int(sched.get("twitter_pull", 600)),
        schedule_rss=int(sched.get("rss_pull", 600)),
    )


# Итерация по *.yml в config/apps → (project_key, cfg, path)
def _iter_project_configs(projects_dir: Path) -> Iterable[Tuple[str, dict, Path]]:
    if not projects_dir.exists():
        log.warning("projects_dir does not exist: %s", projects_dir)
        return
    for yml in sorted(projects_dir.glob("*.yml")):
        try:
            data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            key = (data.get("project_key") or "").strip() or yml.stem
            if key:
                yield key, data, yml
        except Exception:
            log.exception("Bad project config: %s", yml)


# Универсальный вызов адаптера: app.adapters.news.<name>.(pull|fetch|iter_items|run)
def _call_adapter_pull(mod_path: str, project_key: str, app_cfg: dict) -> List[dict]:
    try:
        mod = importlib.import_module(mod_path)
    except Exception as e:
        log.debug("Import adapter failed: %s (%s)", mod_path, e)
        return []
    for fn_name in ("pull", "fetch", "iter_items", "run"):
        fn = getattr(mod, fn_name, None)
        if callable(fn):
            try:
                res = fn(project_key, app_cfg)
                return list(res or [])
            except Exception:
                log.exception("Adapter '%s.%s' crashed", mod_path, fn_name)
                return []
    log.debug("Adapter %s has no suitable callable", mod_path)
    return []


# Предочистка "сырого" элемента от источника (url/ts/id/source/project_key)
def _normalize_source_item(raw: dict, project_key: str, source: str) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    item = dict(raw)

    # URL: легкая гигиена
    u = item.get("url") or item.get("link") or item.get("source_url") or ""
    if u:
        u = normalize_url(u)
        if u:
            u = _strip_tracking_params(u)
        item["url"] = u or None

    # Время: unix → ISO, иначе оставляем строку как есть
    ts = item.get("ts") or item.get("timestamp")
    if isinstance(ts, (int, float)):
        item["ts"] = datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    elif isinstance(ts, str):
        item["ts"] = ts
    else:
        item.pop("ts", None)

    # Детерминированный id, если не задан
    if not item.get("id"):
        base = f"{project_key}:{source}:{item.get('channel') or ''}:{item.get('url') or item.get('title') or ''}"
        item["id"] = str(abs(hash(base)))

    # Источник/проект
    item["source"] = source
    item["project_key"] = project_key

    return item


# Применение строгой схемы (если есть core.news.schema)
def _apply_schema(item: dict) -> dict:
    try:
        from core.news import schema as news_schema

        return news_schema.shape_item(item)
    except Exception:
        # если схемы нет/упала — возвращаем как есть (сигнал в логи)
        return item


# Slack: через адаптер (если есть), иначе пусто
def _pull_slack(project_key: str, app_cfg: dict) -> List[dict]:
    src_cfg = (app_cfg.get("sources") or {}).get("slack") or {}
    if not src_cfg.get("enabled", False):
        return []
    items = _call_adapter_pull("app.adapters.news.slack", project_key, app_cfg)
    out: List[dict] = []
    for raw in items or []:
        norm = _normalize_source_item(raw, project_key, "slack")
        if norm:
            out.append(_apply_schema(norm))
    return out


# X(Twitter): сначала пробуем адаптер, если пусто/нет — fallback на core.parser.twitter.get_recent_tweets
def _pull_twitter(project_key: str, app_cfg: dict) -> List[dict]:
    src_cfg = (app_cfg.get("sources") or {}).get("twitter") or {}
    if not src_cfg.get("enabled", False):
        return []

    # 1) Попытка через внешний адаптер
    items = _call_adapter_pull("app.adapters.news.twitter", project_key, app_cfg)
    if items:
        out: List[dict] = []
        for raw in items:
            norm = _normalize_source_item(raw, project_key, "twitter")
            if norm:
                out.append(_apply_schema(norm))
        return out

    # 2) Fallback: напрямую через парсер (твои новые функции)
    try:
        from core.parser.twitter import get_recent_tweets
    except Exception:
        log.exception("core.parser.twitter import failed")
        return []

    handles = list(src_cfg.get("handles") or [])
    handle_limit = int(src_cfg.get("handle_limit") or 5)
    oldest_days = int(src_cfg.get("oldest_days") or 0) or None

    try:
        tweets = get_recent_tweets(
            handles, handle_limit=handle_limit, oldest_days=oldest_days
        )
    except Exception:
        log.exception("get_recent_tweets crashed")
        tweets = []

    out: List[dict] = []
    for tw in tweets or []:
        # Приводим к унифицированной структуре; текст кладём в body, url — статус X
        item = {
            "id": f"tw:{tw.get('handle','')}:{tw.get('id','')}",
            "title": tw.get("title") or "",
            "body": tw.get("text") or "",
            "url": tw.get("status_url") or "",
            "ts": tw.get("datetime") or None,
            "author": tw.get("handle") or "",
            "media": tw.get("media") or [],
            "channel": tw.get("handle")
            or "",  # чтобы попал в базовый id-хэш при отсутствии id
            "source": "twitter",
            "project_key": project_key,
        }
        # Нормализация + схема
        norm = _normalize_source_item(item, project_key, "twitter")
        if norm:
            out.append(_apply_schema(norm))

    return out


# RSS: через адаптер (если есть), иначе пусто
def _pull_rss(project_key: str, app_cfg: dict) -> List[dict]:
    src_cfg = (app_cfg.get("sources") or {}).get("rss") or {}
    if not src_cfg.get("enabled", False):
        return []
    items = _call_adapter_pull("app.adapters.news.rss", project_key, app_cfg)
    out: List[dict] = []
    for raw in items or []:
        norm = _normalize_source_item(raw, project_key, "rss")
        if norm:
            out.append(_apply_schema(norm))
    return out


# Собрать все источники по проекту
def _pull_all_sources_for_project(project_key: str, app_cfg: dict) -> List[dict]:
    collected: List[dict] = []
    collected += _pull_slack(project_key, app_cfg)
    collected += _pull_twitter(project_key, app_cfg)
    collected += _pull_rss(project_key, app_cfg)
    return collected


# Сохранение через core.storage.save_news_item; возвращает (saved, skipped)
def _persist_items(items: List[dict], *, dry_run: bool) -> Tuple[int, int]:
    saved = skipped = 0
    for it in items:
        try:
            if dry_run:
                skipped += 1
                continue
            uid = str(it.get("id") or "")
            project_key = str(it.get("project_key") or "project")
            when = _parse_iso_to_dt(it.get("ts"))
            save_news_item(project_key, uid, it, when=when)
            saved += 1
        except Exception:
            skipped += 1
            log.exception("Save failed for item id=%s", it.get("id"))
    return saved, skipped


# ISO → datetime
def _parse_iso_to_dt(v: Any) -> Optional[datetime]:
    if isinstance(v, str):
        try:
            s = v[:-1] if v.endswith("Z") else v
            return datetime.fromisoformat(s)
        except Exception:
            return None
    return None


# Главный вход: пробежать проекты, собрать, сохранить
def run_news_once() -> dict:
    settings = _load_settings()
    cfg = _parse_news_mode(settings)

    if not cfg.enabled:
        log.info("news_aggregator disabled in settings.yml")
        return {"ok": True, "enabled": False, "saved": 0, "skipped": 0}

    total_saved = 0
    total_skipped = 0
    apps = list(_iter_project_configs(cfg.projects_dir))

    if not apps:
        log.info("no app configs found at %s", cfg.projects_dir)
        return {"ok": True, "enabled": True, "saved": 0, "skipped": 0}

    for project_key, app_cfg, yml_path in apps:
        try:
            items = _pull_all_sources_for_project(project_key, app_cfg)
            saved, skipped = _persist_items(items, dry_run=cfg.dry_run)
            total_saved += saved
            total_skipped += skipped
            log.info(
                "news: %s  saved=%s skipped=%s dry_run=%s (file=%s)",
                project_key,
                saved,
                skipped,
                cfg.dry_run,
                yml_path.name,
            )
        except Exception:
            log.error("project '%s' crashed:\n%s", project_key, traceback.format_exc())

    return {
        "ok": True,
        "enabled": True,
        "saved": total_saved,
        "skipped": total_skipped,
        "dry_run": cfg.dry_run,
    }


# CLI-обёртка: локальный прогон → `python -m core.news.runner`
def main():
    res = run_news_once()
    log.info("cli.news finished: %s", res)
    print(res)


if __name__ == "__main__":
    main()
