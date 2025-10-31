from __future__ import annotations

import importlib
import re
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

import yaml
from core.log_setup import get_logger
from core.normalize import _strip_tracking_params, force_https, normalize_url
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

    # URL: бережно обрабатываем статусные X/Twitter-ссылки
    u = item.get("url") or item.get("link") or item.get("source_url") or ""
    if u:
        try:
            from urllib.parse import urlparse

            p = urlparse(u)
            host = (p.netloc or "").lower().replace("www.", "")
            path = p.path or ""
            if host in {"x.com", "twitter.com"} and "/status/" in path:
                # статусный URL - оставляем как есть, только https + без завершающего слеша
                item["url"] = force_https(u).rstrip("/")
            else:
                uu = normalize_url(u)
                if uu:
                    uu = _strip_tracking_params(uu)
                item["url"] = uu or None
        except Exception:
            item["url"] = force_https(u).rstrip("/")

    # время: unix → ISO, иначе оставляем строку как есть
    ts = item.get("ts") or item.get("timestamp")
    if isinstance(ts, (int, float)):
        item["ts"] = datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    elif isinstance(ts, str):
        item["ts"] = ts
    else:
        item.pop("ts", None)

    # детерминированный id, если не задан
    if not item.get("id"):
        base = f"{project_key}:{source}:{item.get('channel') or ''}:{item.get('url') or item.get('title') or ''}"
        item["id"] = str(abs(hash(base)))

    # источник/проект
    item["source"] = source
    item["project"] = project_key

    return item


# Применение строгой схемы (если есть core.news.schema)
def _apply_schema(item: dict) -> dict:
    try:
        from core.news import schema as news_schema

        return news_schema.shape_item(item)
    except Exception:
        # если схемы нет/упала — возвращаем как есть (сигнал в логи)
        return item


# Сворачивание Slack-реплаев (thread_ts) под родительское сообщение
def _fold_slack_threads(items: List[dict]) -> List[dict]:
    if not items:
        return []

    # индексы по ts и по thread_ts
    by_ts: dict[str, dict] = {}
    children: dict[str, list[dict]] = {}

    for it in items:
        ts = str(it.get("thread_ts") or it.get("ts") or "")
        if not it.get("thread_ts") or str(it.get("thread_ts")) == str(it.get("ts")):
            by_ts[str(it.get("ts"))] = it
        else:
            children.setdefault(str(it.get("thread_ts")), []).append(it)

    out: List[dict] = []
    for parent_ts, parent in by_ts.items():
        replies = children.get(parent_ts, [])
        # строим body
        main_text = (parent.get("body") or parent.get("text") or "").strip()
        lines = [main_text] if main_text else []
        if replies:
            lines += ["", f"--- Thread ({len(replies)}):"]
            for r in replies:
                rtext = (r.get("body") or r.get("text") or "").strip()
                rlink = (r.get("permalink") or r.get("url") or "").strip()
                if rtext and rlink:
                    lines.append(f" • {rtext}  [{rlink}]")
                elif rtext:
                    lines.append(f" • {rtext}")
                elif rlink:
                    lines.append(f" • {rlink}")
        merged = dict(parent)
        merged["body"] = "\n".join(lines).strip()
        # сырые реплаи в extra.thread (если extra нет - создаем)
        extra = dict(merged.get("extra") or {})
        extra["thread"] = replies
        merged["extra"] = extra
        out.append(merged)

    # реплаи как отдельные записи в ленту не пушим
    return out


# Slack: через адаптер (если есть), иначе пусто
def _pull_slack(project_key: str, app_cfg: dict) -> List[dict]:
    src_cfg = (app_cfg.get("sources") or {}).get("slack") or {}
    if not src_cfg.get("enabled", False):
        return []
    items = _call_adapter_pull("app.adapters.news.slack", project_key, app_cfg)

    # нормализуем адаптерные записи
    prelim: List[dict] = []
    for raw in items or []:
        norm = _normalize_source_item(raw, project_key, "slack")
        if norm:
            prelim.append(_apply_schema(norm))

    # сворачиваем треды Slack под родителя
    folded = _fold_slack_threads(prelim)
    return folded


# X(Twitter): сначала пробуем адаптер, если пусто/нет - fallback на core.parser.twitter.get_recent_tweets
def _pull_twitter(project_key: str, app_cfg: dict) -> List[dict]:
    src_cfg = (app_cfg.get("sources") or {}).get("twitter") or {}
    if not src_cfg.get("enabled", False):
        return []

    # попытка через внешний адаптер
    items = _call_adapter_pull("app.adapters.news.twitter", project_key, app_cfg)
    if items:
        out: List[dict] = []
        for raw in items:
            norm = _normalize_source_item(raw, project_key, "twitter")
            if norm:
                out.append(_apply_schema(norm))
        return out

    # fallback: напрямую через парсер (твои новые функции)
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
        # вставляем тред в body
        body = _build_twitter_body(tw.get("text") or "", tw.get("thread") or [])

        # теги: из основного текста + строк треда
        thread_text = "\n".join((r.get("text") or "") for r in (tw.get("thread") or []))
        tags = _extract_hashtags(f"{tw.get('text') or ''}\n{thread_text}")

        # attachments: m3u8 + постер из страницы статуса (через nitter)
        attachments = list(tw.get("attachments") or [])

        # fallback на URL статуса, если парсер его не дал
        status_url = (tw.get("status_url") or "").strip()
        if not status_url:
            h = (tw.get("handle") or "").strip()
            tid = (tw.get("id") or "").strip()
            if h and tid:
                status_url = f"https://x.com/{h}/status/{tid}"

        item = {
            "id": f"tw:{tw.get('handle','')}:{tw.get('id','')}",
            "title": tw.get("title") or "",
            "body": body,
            "url": status_url or tw.get("url") or "",
            "ts": tw.get("datetime") or None,
            "author": tw.get("handle") or "",
            "channel": tw.get("handle") or "",
            "source": "twitter",
            "tags": tags,
            "attachments": attachments,
            "project": project_key,
            "extra": {"thread": tw.get("thread") or []},
        }
        norm = _normalize_source_item(item, project_key, "twitter")
        if norm:
            out.append(_apply_schema(norm))

    return out


# Сбор хештегов
def _extract_hashtags(text: str) -> list[str]:
    s = (text or "").strip()
    if not s:
        return []
    # берем #слово из юникод-символов (\w в Python включает кириллицу при re.UNICODE)
    tags = [
        m.group(1).strip("_").lower()
        for m in re.finditer(r"#([\w]{2,50})", s, re.UNICODE)
    ]
    # компактим и дедупим, сохраняя порядок
    out, seen = [], set()
    for t in tags:
        if t and t not in seen:
            out.append(t)
            seen.add(t)
    return out


# Сбор body: основной твит + блок Thread(N) со ссылками на ответы автора
def _build_twitter_body(main_text: str, thread: list[dict]) -> str:
    main = (main_text or "").strip()
    if not thread:
        return main
    lines = [main, "", f"--- Thread ({len(thread)}):"]
    for r in thread:
        t = (r.get("text") or "").strip()
        link = (r.get("status_url") or "").strip()
        if t and link:
            lines.append(f" • {t}  [{link}]")
        elif t:
            lines.append(f" • {t}")
        elif link:
            lines.append(f" • {link}")
    return "\n".join(lines)


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
            project_key = str(it.get("project") or "project")
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
            try:
                items.sort(key=lambda x: (x.get("ts") or ""), reverse=True)
            except Exception:
                pass
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


# CLI-обертка: локальный прогон → `python -m core.news.runner`
def main():
    res = run_news_once()
    log.info("cli.news finished: %s", res)
    print(res)


if __name__ == "__main__":
    main()
