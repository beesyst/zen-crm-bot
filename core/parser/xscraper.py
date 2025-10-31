from __future__ import annotations

# Коротко: импорты и базовые константы/логгер/доступ к конфигу.
import dataclasses
import datetime as _dt
import json
import os
import re
import time
from typing import Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import filelock
import requests
from bs4 import BeautifulSoup
from core.log_setup import get_logger
from core.normalize import force_https
from core.settings import get_http_ua, get_nitter_cfg, get_settings

logger = get_logger("scraper")
UA = get_http_ua()

# --- Встроенный дефолтный Bearer (можно переопределить в config/parser.xscraper.bearer) ---
_DEFAULT_BEARER = (
    "Bearer "
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs="
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# --- Глобальные GraphQL URL X ---
_USER_BY_SCREEN_NAME = (
    "https://twitter.com/i/api/graphql/7mjxD3-C6BxitPMVQ6w0-Q/UserByScreenName"
)
_USER_TWEETS_AND_REPLIES = (
    "https://twitter.com/i/api/graphql/BSKxQ9_IaCoVyIvQHQROIQ/UserTweetsAndReplies"
)
_GUEST_ACTIVATE = "https://api.twitter.com/1.1/guest/activate.json"

# --- Pre-compiled regex для скорости ---
_RE_HANDLE = re.compile(
    r"^https?://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})/?$", re.I
)
_RE_STATUS = re.compile(r"/status/(\d+)", re.I)


# --------- БЛОК ВСПОМОГАТЕЛЬНЫХ: КОНФИГ / ДИРЕКТОРИИ / HOST ---------


# Возвращает нормализованный конфиг parser.xscraper из settings.yml (с дефолтами)
def _xs_cfg() -> Dict[str, object]:
    cfg = (get_settings().get("parser") or {}).get("xscraper") or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "bearer": str(cfg.get("bearer") or "").strip() or _DEFAULT_BEARER,
        "guest_cache_dir": str(cfg.get("guest_cache_dir") or "storage/twitter").strip(),
        "timeout_sec": int(cfg.get("timeout_sec") or 15),
        "retries": int(cfg.get("retries") or 2),
        "tweet_limit": int(cfg.get("tweet_limit") or 5),
        "oldest_days": int(cfg.get("oldest_days") or 0),
        "expand_short_links": bool(cfg.get("expand_short_links", True)),
        "proxy": str(cfg.get("proxy") or "").strip(),
        "playwright_timeout_sec": int(cfg.get("playwright_timeout_sec") or 90),
        "save_to_storage": bool(cfg.get("save_to_storage", False)),
        "storage_dir": str(cfg.get("storage_dir") or "storage/news").strip(),
    }


# Обеспечиваем существование директории
def _ensure_dir(d: str) -> None:
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass


# Достаём host из URL
def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# --------- НОРМАЛИЗАЦИЯ X/TWITTER ---------


# Нормализуем профиль к https://x.com/<handle>
def normalize_twitter_url(u: str | None) -> str:
    if not u:
        return ""
    s = force_https(u.strip())
    s = re.sub(r"^https://twitter\.com", "https://x.com", s, flags=re.I)
    s = re.sub(r"[?#].*$", "", s)
    s = re.sub(
        r"/(photo|media|with_replies|likes|lists|following|followers)/?$",
        "",
        s,
        flags=re.I,
    )
    s = re.sub(r"/status/\d+(?:/photo/\d+)?$", "", s, flags=re.I)
    s = re.sub(r"/i/(?:[^/]+)(?:/)?$", "", s, flags=re.I)
    s = s.rstrip("/")
    m = _RE_HANDLE.match(s + "/")
    return f"https://x.com/{m.group(1)}" if m else s


# Нормализуем URL аватара (nitter /pic/ → прямой pbs.twimg.com, апскейл до 400x400)
def normalize_twitter_avatar(url: str | None) -> str:
    u = force_https(url or "")
    if not u:
        return ""
    try:
        p = urlparse(u)
        if "/pic/" in (p.path or ""):
            u = _decode_nitter_pic_url(p.path)
    except Exception:
        pass
    if u.startswith("/pic/"):
        u = _decode_nitter_pic_url(u)
    if u.startswith("pbs.twimg.com/"):
        u = "https://" + u
    u = re.sub(r"(?:\?[^#]*)?(?:#.*)?$", "", u)
    try:
        p = urlparse(u)
        if (p.netloc or "").endswith("pbs.twimg.com"):
            u = re.sub(
                r"_(?:normal|bigger|mini|200x200)\.(jpg|png)$",
                r"_400x400.\1",
                u,
                flags=re.I,
            )
    except Exception:
        pass
    return u


# Декодируем nitter-путь /pic/<encoded> в полноценный https://...
def _decode_nitter_pic_url(src: str) -> str:
    s = (src or "").strip()
    if s.startswith("/pic/"):
        s = s[len("/pic/") :]
    s = requests.utils.unquote(s)
    if s.startswith("//"):
        s = "https:" + s
    elif s.startswith("http://"):
        s = "https://" + s[7:]
    elif not s.startswith("https://"):
        s = "https://" + s.lstrip("/")
    return s


# --------- ГОСТЕВОЙ ТОКЕН X: КЭШ + ОСВЕЖЕНИЕ ---------


@dataclasses.dataclass
class _GuestToken:
    token: Optional[str] = None
    set_time: float = 0.0
    validity: int = 10800  # 3 часа

    def fresh(self) -> bool:
        return bool(self.token) and (time.time() - self.set_time < self.validity)


# Менеджер гост-токена с кэшем в storage/twitter/guest_token.json
class GuestTokenManager:
    # Инициализируем путь к файлам блокировки и кэша токена
    def __init__(self):
        cfg = _xs_cfg()
        base = cfg["guest_cache_dir"] or "storage/twitter"
        _ensure_dir(str(base))
        self._file = os.path.join(str(base), "guest_token.json")
        self._lock = filelock.FileLock(self._file + ".lock")
        self._token = _GuestToken()

    # Прочитать токен из файла (если не свежий — вернет пустой)
    def _read(self) -> None:
        try:
            with self._lock:
                if not os.path.exists(self._file):
                    return
                with open(self._file, "r", encoding="utf-8") as f:
                    obj = json.load(f)
            self._token = _GuestToken(
                token=obj.get("token") or None,
                set_time=float(obj.get("set_time") or 0.0),
            )
        except Exception:
            self._token = _GuestToken()

    # Записать токен в файл
    def _write(self) -> None:
        try:
            with self._lock:
                with open(self._file, "w", encoding="utf-8") as f:
                    json.dump(
                        {"token": self._token.token, "set_time": self._token.set_time},
                        f,
                    )
        except Exception:
            pass

    # Получить актуальный токен (освежит при необходимости)
    def get(self, force_refresh: bool = False) -> Optional[str]:
        if not force_refresh:
            if self._token.fresh():
                return self._token.token
            self._read()
            if self._token.fresh():
                return self._token.token
        return self.refresh()

    # Вызвать guest/activate и сохранить токен
    def refresh(self) -> Optional[str]:
        cfg = _xs_cfg()
        headers = {"Authorization": cfg["bearer"], "User-Agent": UA}
        proxies = (
            {"http": cfg["proxy"], "https": cfg["proxy"]} if cfg["proxy"] else None
        )
        try:
            r = requests.post(
                _GUEST_ACTIVATE,
                headers=headers,
                timeout=cfg["timeout_sec"],
                proxies=proxies,
            )
            r.raise_for_status()
            tok = (r.json() or {}).get("guest_token")
            if tok:
                self._token = _GuestToken(token=tok, set_time=time.time())
                self._write()
                return tok
        except Exception as e:
            logger.warning("Guest token refresh failed: %s", e)
        return None


# --------- HTTP/GraphQL ВСПОМОГАТЕЛЬНЫЕ ---------


# Сборка заголовков API X с гост-токеном
def _api_headers(gt: str) -> Dict[str, str]:
    return {
        "Authorization": _xs_cfg()["bearer"],
        "x-guest-token": gt,
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://x.com/",
    }


# Универсальный JSON-запрос с ретраями и авто-обновлением guest token при 403/429
def _request_json(url: str, params: dict, gt_mgr: GuestTokenManager) -> dict:
    cfg = _xs_cfg()
    proxies = {"http": cfg["proxy"], "https": cfg["proxy"]} if cfg["proxy"] else None

    last_err = None
    # Первая попытка — с текущим токеном/только что обновленным
    token = gt_mgr.get() or gt_mgr.refresh()
    for attempt in range(cfg["retries"] + 1):
        try:
            headers = _api_headers(token or "")
            r = requests.get(
                url,
                headers=headers,
                params={"variables": json.dumps(params, separators=(",", ":"))},
                timeout=cfg["timeout_sec"],
                proxies=proxies,
            )
            if r.status_code in (403, 429):
                # Пробуем один раз обновить токен и повторить
                token = gt_mgr.refresh()
                headers = _api_headers(token or "")
                r = requests.get(
                    url,
                    headers=headers,
                    params={"variables": json.dumps(params, separators=(",", ":"))},
                    timeout=cfg["timeout_sec"],
                    proxies=proxies,
                )
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            if "application/json" not in ct:
                raise ValueError(f"Non-JSON content-type: {ct}")
            return r.json() or {}
        except Exception as e:
            last_err = e
            # экспоненциальная задержка
            if attempt < cfg["retries"]:
                time.sleep(1.0 * (2**attempt))
    raise RuntimeError(f"GraphQL request failed after retries: {last_err}")


# --------- GraphQL: ПОЛУЧЕНИЕ user_id + ЛЕНТЫ ---------


# Добываем rest_id пользователя по handle
def _fetch_user_id(handle: str, gt_mgr: GuestTokenManager) -> Optional[str]:
    params = {"screen_name": handle, "withSafetyModeUserFields": True}
    data = _request_json(_USER_BY_SCREEN_NAME, params, gt_mgr)
    result = ((data.get("data") or {}).get("user") or {}).get("result") or {}
    rest_id = result.get("rest_id")
    return str(rest_id) if rest_id else None


# Распаковываем Tweet-entries из instructions в список словарей (см. формат Nitter)
def _extract_tweets_from_instructions(
    instructions: list, handle: str, limit: int
) -> List[dict]:
    items: List[dict] = []
    for instr in instructions:
        if instr.get("type") != "TimelineAddEntries":
            continue
        for entry in instr.get("entries", []):
            try:
                if not str(entry.get("entryId", "")).startswith("tweet-"):
                    continue
                content = entry["content"]
                if content.get("entryType") != "TimelineTimelineItem":
                    continue
                item = content["itemContent"]
                if item.get("itemType") != "TimelineTweet":
                    continue
                result = item["tweet_results"]["result"]
                # в result может быть Tweet или TweetWithVisibilityResults → берём legacy
                legacy = result.get("legacy") or (result.get("tweet", {}).get("legacy"))
                if not legacy:
                    continue

                tid = legacy.get("id_str") or legacy.get("id")
                full_text = legacy.get("full_text") or legacy.get("text") or ""
                created = legacy.get("created_at") or ""
                # конвертируем дату X: "Mon Oct 28 08:11:21 +0000 2025" → ISO
                try:
                    dt_iso = _dt.datetime.strptime(
                        created, "%a %b %d %H:%M:%S %z %Y"
                    ).isoformat()
                except Exception:
                    dt_iso = ""

                text = re.sub(r"\s+", " ", full_text).strip()
                title = (text[:117] + "…") if len(text) > 120 else text

                # media (по возможности)
                media = []
                ext = legacy.get("extended_entities") or {}
                for m in ext.get("media") or []:
                    src = force_https(
                        m.get("media_url_https") or m.get("media_url") or ""
                    )
                    if src:
                        media.append(src)
                media = list(dict.fromkeys(media))

                status_url = f"https://x.com/{handle}/status/{tid}"
                items.append(
                    {
                        "id": str(tid or ""),
                        "status_url": status_url,
                        "handle": handle,
                        "datetime": dt_iso,
                        "text": text,
                        "title": title,
                        "media": media,
                    }
                )
                if len(items) >= limit:
                    return items
            except Exception:
                continue
    return items


# Забираем ленту твитов по user_id через GraphQL
def _fetch_user_tweets(
    user_id: str, handle: str, limit: int, gt_mgr: GuestTokenManager
) -> List[dict]:
    variables = {
        "userId": user_id,
        "count": max(50, limit * 5),  # берём запас, отфильтруем ниже
        "includePromotedContent": False,
        "withCommunity": True,
        "withSuperFollowsUserFields": True,
        "withDownvotePerspective": False,
        "withReactionsMetadata": False,
        "withReactionsPerspective": False,
        "withSuperFollowsTweetFields": True,
        "withVoice": True,
        "withV2Timeline": False,
    }
    data = _request_json(_USER_TWEETS_AND_REPLIES, variables, gt_mgr)
    instructions = (
        ((data.get("data") or {}).get("user") or {}).get("result") or {}
    ).get("timeline", {}).get("timeline", {}).get("instructions", []) or []
    return _extract_tweets_from_instructions(instructions, handle, limit)


# --------- FALLBACK: PLAYWRIGHT (x.com → HTML) ---------


# Внутренний запуск node playwright.js (структура совместима с твоим окружением)
def _run_playwright_x(u: str, timeout_sec: int) -> dict:
    script = os.path.join(os.path.dirname(__file__), "playwright.js")
    try:
        import subprocess

        res = subprocess.run(
            [
                "node",
                script,
                "--url",
                u,
                "--timeout",
                str(int(timeout_sec * 1000)),
                "--retries",
                "2",
                "--wait",
                "networkidle",
                "--ua",
                UA or "",
                "--twitterProfile",
            ],
            cwd=os.path.dirname(script),
            capture_output=True,
            text=True,
            timeout=timeout_sec + 15,
        )
        raw = (res.stdout or "").strip()
        return json.loads(raw) if raw.startswith("{") else {}
    except Exception as e:
        logger.warning("playwright.js run error for %s: %s", u, e)
        return {}


# Разбор HTML X-профиля в список твитов (минимальный, чтобы выдержать фолбек)
def _extract_x_tweets_from_html(html: str, handle: str, limit: int) -> List[dict]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items: List[dict] = []
    for art in soup.find_all("article"):
        try:
            t = art.select_one("time[datetime]")
            if not t:
                continue
            dt = (t.get("datetime") or "").strip()

            a = art.select_one("a[href*='/status/']")
            href = (a.get("href") or "").strip() if a else ""
            m = _RE_STATUS.search(href)
            tw_id = m.group(1) if m else ""
            if not tw_id:
                continue

            text = re.sub(r"\s+", " ", art.get_text(" ", strip=True)).strip()
            title = (text[:117] + "…") if len(text) > 120 else text

            media = []
            for img in art.select("img[src]"):
                src = (img.get("src") or "").strip()
                if src and "twimg.com" in src:
                    media.append(force_https(src))
            media = list(dict.fromkeys(media))

            status_url = f"https://x.com/{handle}/status/{tw_id}"
            items.append(
                {
                    "id": tw_id,
                    "status_url": status_url,
                    "handle": handle,
                    "datetime": dt,
                    "text": text,
                    "title": title,
                    "media": media,
                }
            )
        except Exception:
            continue
        if len(items) >= max(1, limit):
            break
    return items


# --------- УТИЛИТЫ: ССЫЛКИ/SHORTENERS/ФИЛЬТР ПО ДАТЕ ---------


# Разворачиваем короткие ссылки и чистим UTM (исп-ся для BIO/в перспективе)
def _expand_short_links(urls: list[str]) -> list[str]:
    cfg = _xs_cfg()
    if not cfg["expand_short_links"]:
        return [force_https(u).rstrip("/") for u in (urls or [])]

    SHORTENERS = {
        "t.co",
        "bit.ly",
        "tinyurl.com",
        "ow.ly",
        "buff.ly",
        "t.ly",
        "shorturl.at",
    }
    out = []
    proxies = {"http": cfg["proxy"], "https": cfg["proxy"]} if cfg["proxy"] else None
    for u in urls or []:
        try:
            u = force_https(u)
            h = _host(u)
            if h in SHORTENERS:
                r = requests.get(
                    u,
                    headers={"User-Agent": UA, "Referer": "https://x.com/"},
                    timeout=cfg["timeout_sec"],
                    allow_redirects=True,
                    proxies=proxies,
                )
                final = force_https(r.url or u)
                p = urlparse(final)
                clean_q = [
                    (k, v)
                    for (k, v) in parse_qsl(p.query, keep_blank_values=True)
                    if k.lower()
                    not in {
                        "utm_source",
                        "utm_medium",
                        "utm_campaign",
                        "utm_term",
                        "utm_content",
                        "ref",
                        "source",
                        "s",
                    }
                ]
                final = urlunparse(
                    (
                        p.scheme,
                        p.netloc,
                        p.path.rstrip("/"),
                        p.params,
                        urlencode(clean_q),
                        "",
                    )
                )
                out.append(final)
            else:
                out.append(u.rstrip("/"))
        except Exception:
            out.append(force_https(u).rstrip("/"))
    # дедуп
    seen, deduped = set(), []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


# Фильтр твитов по возрасту (oldest_days)
def _filter_oldest(items: List[dict], oldest_days: int) -> List[dict]:
    if not oldest_days:
        return items
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=int(oldest_days))
    out = []
    for it in items or []:
        try:
            dt = it.get("datetime") or ""
            dt = _dt.datetime.fromisoformat(dt.replace("Z", "+00:00"))
            if dt >= cutoff:
                out.append(it)
        except Exception:
            out.append(it)  # если нет даты — не выкидываем
    return out


# --------- ПУБЛИЧНЫЕ API МОДУЛЯ ---------


# Главный метод: получить последние твиты handle с фолбэками GraphQL → Nitter → Playwright
def fetch_tweets(
    handle: str, limit: Optional[int] = None, oldest_days: Optional[int] = None
) -> List[dict]:
    cfg = _xs_cfg()
    h = (handle or "").strip().lstrip("@")
    if not h:
        return []

    # целевой лимит
    tgt_limit = int(limit or cfg["tweet_limit"] or 5)
    oldest = int(oldest_days or cfg["oldest_days"] or 0)

    # 1) GraphQL (основной путь)
    try:
        gt_mgr = GuestTokenManager()
        uid = _fetch_user_id(h, gt_mgr)
        if uid:
            items = _fetch_user_tweets(uid, h, tgt_limit, gt_mgr)
            items = _filter_oldest(items, oldest)
            logger.info(
                "Scraper GraphQL+parse: https://x.com/%s → tweets=%d", h, len(items)
            )
            if items:
                return items[:tgt_limit]
    except Exception as e:
        logger.info("GraphQL failed for @%s: %s", h, e)

    # 2) Nitter (если включен)
    ncfg = get_nitter_cfg() or {}
    if ncfg.get("enabled"):
        try:
            from core.parser.nitter import fetch_tweets as _nitter_fetch

            items = _nitter_fetch(h, limit=tgt_limit, oldest_days=oldest) or []
            logger.info(
                "Scraper Nitter+parse: https://x.com/%s → tweets=%d", h, len(items)
            )
            if items:
                return items[:tgt_limit]
        except Exception as e:
            logger.info("Nitter fallback failed for @%s: %s", h, e)

    # 3) Playwright (последний шанс)
    try:
        data = (
            _run_playwright_x(f"https://x.com/{h}", cfg["playwright_timeout_sec"]) or {}
        )
        html = (data.get("twitter_profile") or {}).get("html") or data.get("html") or ""
        items = _extract_x_tweets_from_html(html, h, tgt_limit) if html else []
        logger.info(
            "Scraper Playwright+parse: https://x.com/%s → tweets=%d", h, len(items)
        )
        return items[:tgt_limit]
    except Exception as e:
        logger.info("Playwright fallback failed for @%s: %s", h, e)
        return []


# Массовая обёртка: получить твиты по списку handle (слияние, сортировка по времени, дедуп)
def fetch_tweets_bulk(
    handles: List[str],
    per_handle_limit: Optional[int] = None,
    oldest_days: Optional[int] = None,
) -> List[dict]:
    all_items: List[dict] = []
    for h in handles or []:
        all_items.extend(fetch_tweets(h, per_handle_limit, oldest_days) or [])

    # сортируем по дате
    def _k(it):
        try:
            return _dt.datetime.fromisoformat(
                (it.get("datetime") or "").replace("Z", "+00:00")
            )
        except Exception:
            return _dt.datetime.min

    all_items.sort(key=_k, reverse=True)
    # дедуп по (handle,id)
    seen, out = set(), []
    for it in all_items:
        k = (it.get("handle", ""), it.get("id", ""))
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


# Опционально сохраняем список твитов в storage/news/<handle>_tweets.json (если разрешено в конфиге)
def save_to_storage(handle: str, items: List[dict]) -> Optional[str]:
    cfg = _xs_cfg()
    if not cfg.get("save_to_storage"):
        return None
    d = str(cfg.get("storage_dir") or "storage/news").strip()
    _ensure_dir(d)
    path = os.path.join(d, f"{handle.lstrip('@')}_tweets.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items or [], f, ensure_ascii=False, indent=2)
        return path
    except Exception as e:
        logger.info("save_to_storage failed: %s", e)
        return None
