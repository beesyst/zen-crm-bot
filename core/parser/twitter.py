from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Dict, List
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_setup import get_logger
from core.normalize import force_https
from core.parser.nitter import parse_profile
from core.settings import (
    get_http_ua,
    get_social_keys,
)

from .link_aggregator import (
    extract_socials_from_aggregator,
    find_aggregators_in_links,
    verify_aggregator_belongs,
)

logger = get_logger("twitter")
UA = get_http_ua()


# Кэш уже распарсенных профилей X
_PARSED_CACHE: Dict[str, Dict] = {}


# Хелпер: достаем домен из URL без www
def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# Нормализуем ссылку X/Twitter к виду https://x.com/<handle>
def normalize_twitter_url(u: str | None) -> str:
    if not u:
        return ""
    s = force_https((u or "").strip())

    # twitter -> x
    s = re.sub(r"^https://twitter\.com", "https://x.com", s, flags=re.I)

    # убираем query/fragment
    s = re.sub(r"[?#].*$", "", s)

    # срезаем лишние хвосты
    s = re.sub(
        r"/(photo|media|with_replies|likes|lists|following|followers)(?:/)?$",
        "",
        s,
        flags=re.I,
    )
    # важное: срезаем /status/<id> и /i/...
    s = re.sub(r"/status/\d+(?:/photo/\d+)?$", "", s, flags=re.I)
    s = re.sub(r"/i/(?:[^/]+)(?:/)?$", "", s, flags=re.I)

    s = s.rstrip("/")
    m = re.match(r"^https://x\.com/([A-Za-z0-9_]{1,15})$", s, re.I)
    return f"https://x.com/{m.group(1)}" if m else s


# Нормализуем URL аватара X (включая декодирование nitter /pic/)
def normalize_twitter_avatar(url: str | None) -> str:
    u = force_https(url or "")
    if not u:
        return ""

    # nitter absolute or relative: https://nitter.net/pic/... или /pic/...
    try:
        p = urlparse(u)
        if "/pic/" in (p.path or ""):
            # отбросим хост, декодируем по существующей функции
            return _decode_nitter_pic_url(p.path)
    except Exception:
        pass

    if u.startswith("/pic/"):
        u = _decode_nitter_pic_url(u)

    if u.startswith("pbs.twimg.com/"):
        u = "https://" + u

    # убрать query/fragment
    u = re.sub(r"(?:\?[^#]*)?(?:#.*)?$", "", u)
    return u


# Декодируем nitter /pic/<encoded> в прямую https-ссылку
def _decode_nitter_pic_url(src: str) -> str:
    s = (src or "").strip()
    if s.startswith("/pic/"):
        s = s[len("/pic/") :]
    s = unquote(s)
    if s.startswith("//"):
        s = "https:" + s
    elif s.startswith("http://"):
        s = "https://" + s[7:]
    elif s.startswith("https://"):
        pass
    else:
        s = "https://" + s.lstrip("/")
    return s


# Запускаем playwright-скрипт как запасной вариант (прямой X)
def _run_playwright(u: str, timeout: int = 60) -> dict:
    host = urlparse(u).netloc.lower().replace("www.", "")
    if host not in ("x.com", "twitter.com"):
        return {}
    script = os.path.join(os.path.dirname(__file__), "twitter_scraper.js")
    try:
        # twitter_scraper.js ожидает флаги --url и --timeout (мс)
        res = subprocess.run(
            [
                "node",
                script,
                "--url",
                u,
                "--timeout",
                str(int(max(1, timeout) * 1000)),
                "--retries",
                "1",
                "--wait",
                "domcontentloaded",
                "--prefer",
                "x",
                "--ua",
                UA or "",
            ],
            cwd=os.path.dirname(script),
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except Exception as e:
        logger.warning("twitter_scraper.js run error for %s: %s", u, e)
        return {}
    try:
        raw = (res.stdout or "").strip()
        data = json.loads(raw) if raw.startswith("{") else {}
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


# Получаем профиль X: {links, avatar, name} (сперва nitter, затем playwright при необходимости)
def get_links_from_x_profile(
    profile_url: str, need_avatar: bool = True
) -> Dict[str, object]:
    safe = normalize_twitter_url(profile_url or "")
    if not safe:
        return {"links": [], "avatar": "", "name": ""}

    cached = _PARSED_CACHE.get(safe)
    if cached and (not need_avatar or (cached.get("avatar") or "").strip()):
        return cached

    # сначала nitter
    parsed = parse_profile(safe) or {}

    # если nitter не дал вообще ничего или дал без ссылок - пробуем playwright
    if (not parsed) or not (parsed.get("links") or []):
        tries = [safe, safe.rstrip("/") + "/photo"] if need_avatar else [safe]
        for u in tries:
            data = _run_playwright(u)
            avatar_js = (data.get("avatar") or "") or (
                (data.get("images") or {}).get("avatar") or ""
            )
            if data.get("links") or avatar_js or data.get("name"):
                logger.info(
                    "Playwright direct GET+parse: %s → avatar=%s, links=%d",
                    u,
                    "yes" if (avatar_js or "").strip() else "no",
                    len(data.get("links") or []),
                )
                parsed = {
                    "links": data.get("links") or [],
                    "avatar": normalize_twitter_avatar(avatar_js or ""),
                    "name": data.get("name") or "",
                }
                break

    # если nitter что-то дал, но нет аватара, а он нужен - playwright
    elif need_avatar and not (parsed.get("avatar") or "").strip():
        tries = [safe, safe.rstrip("/") + "/photo"]
        for u in tries:
            data = _run_playwright(u)
            avatar_js = (data.get("avatar") or "") or (
                (data.get("images") or {}).get("avatar") or ""
            )
            if data.get("links") or avatar_js or data.get("name"):
                logger.info(
                    "Playwright avatar fallback: %s → avatar=%s, links=%d",
                    u,
                    "yes" if (avatar_js or "").strip() else "no",
                    len(data.get("links") or []),
                )
                parsed = {
                    "links": parsed.get("links") or [] or data.get("links") or [],
                    "avatar": normalize_twitter_avatar(
                        avatar_js or parsed.get("avatar") or ""
                    ),
                    "name": parsed.get("name") or data.get("name") or "",
                }
                break

    out = {
        "links": parsed.get("links") or [],
        "avatar": normalize_twitter_avatar(parsed.get("avatar") or ""),
        "name": parsed.get("name") or "",
    }

    _PARSED_CACHE[safe] = out
    return out


# Достаем все кандидаты X-профилей из HTML (ссылки и голый текст)
def extract_twitter_profiles(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    profiles: set[str] = set()

    # из ссылок <a>
    for a in soup.find_all("a", href=True):
        raw = urljoin(base_url, a["href"])
        if not re.search(r"(?:^|//)(?:[^/]*\.)?(?:twitter\.com|x\.com)/", raw, re.I):
            continue

        try:
            p = urlparse(raw)
            path = p.path or "/"
            # отбрасываем служебные/непрофильные пути
            if re.search(
                r"/(?:status/|share|intent|search|hashtag|i/|home|messages|explore|notifications)(?:/|$)",
                path,
                re.I,
            ):
                continue

            # матчим /<handle>(/?) без хвостов
            m = re.match(r"^/([A-Za-z0-9_]{1,15})/?$", path)
            if not m:
                continue
            handle = m.group(1)

            # канонизируем к https://x.com/<handle>
            clean = f"https://x.com/{handle}"
            profiles.add(force_https(clean))
        except Exception:
            continue

    # из голого текста (полные url) - тоже строгая валидация
    text = html or ""
    for m in re.finditer(
        r"https?://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_/])",
        text,
        re.I,
    ):
        try:
            handle = m.group(1)
            clean = f"https://x.com/{handle}"
            profiles.add(force_https(clean))
        except Exception:
            pass

    return list(profiles)


# Быстро проверяем, что профиль живой: есть хотя бы одна ссылка в BIO
def _is_valid_profile(parsed: dict) -> bool:
    if not isinstance(parsed, dict):
        return False
    links = parsed.get("links") or []
    return len(links) >= 1


# Проверяем твиттер и пробуем домержить соцсети через агрегатор из BIO
def verify_twitter_and_enrich(
    twitter_url: str, site_domain: str
) -> tuple[bool, dict, str]:
    # нормализуем входной твиттер → x.com
    twitter_url = normalize_twitter_url(twitter_url or "")
    data = get_links_from_x_profile(twitter_url, need_avatar=False)

    if not _is_valid_profile(data):
        return False, {}, ""

    # прямой офсайт в bio → подтверждаем X
    site_domain_norm = (site_domain or "").lower().lstrip(".")
    bio_links = [force_https(b).rstrip("/") for b in (data.get("links") or [])]

    # отмечаем, что X подтвержден по офсайту
    confirmed_by_site = False
    for b in bio_links:
        try:
            if site_domain_norm and _host(b).endswith(site_domain_norm):
                confirmed_by_site = True
                break
        except Exception:
            pass

    m = re.match(
        r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$",
        (twitter_url or "") + "/",
        re.I,
    )
    handle = m.group(1) if m else ""
    aggs = find_aggregators_in_links(bio_links)

    enriched_bits: dict = {}
    agg_used = ""

    for agg in aggs:
        agg_norm = force_https(agg)
        ok, bits = verify_aggregator_belongs(agg_norm, site_domain_norm, handle)
        if ok:
            enriched_bits = bits or {}
            agg_used = agg_norm
            break

    # если X подтвержден по сайту или по агрегатору - лог один раз и возвращаем
    if confirmed_by_site or enriched_bits:
        if confirmed_by_site and site_domain_norm and not enriched_bits.get("website"):
            enriched_bits["website"] = f"https://{site_domain_norm}/".replace(
                "//www.", "//"
            )
        logger.info("X подтвержден: %s", twitter_url)
        return True, enriched_bits, agg_used

    # Нормализуем соцссылки из агрегатора по списку ключей из конфига
    def _normalize_socials(d: dict) -> dict:
        out = {}
        allowed = set(
            get_social_keys()
        )  # ← источник правды: settings.yml: socials.keys
        for k, v in (d or {}).items():
            if not isinstance(v, str) or not v:
                continue
            vv = force_https(v)
            # для ключа twitter приводим домен к x.com
            if k == "twitter":
                vv = vv.replace("twitter.com", "x.com")
            # пропускаем только разрешенные ключи
            if k in allowed:
                out[k] = vv
        return out

    # Проверяем каждый агрегатор: жестко → мягко → soft-policy из BIO
    for agg in aggs:
        agg_norm = force_https(agg)

        # жёсткая проверка принадлежности
        ok, bits = verify_aggregator_belongs(agg_norm, site_domain_norm, handle)

        # мягкая проверка по содержимому (без домена/handle)
        if not ok:
            try_bits = extract_socials_from_aggregator(agg_norm) or {}
            soft_has_site = False
            soft_has_handle = False

            # офсайт по домену
            for v in try_bits.values():
                try:
                    if (
                        isinstance(v, str)
                        and v
                        and site_domain_norm
                        and _host(v).endswith(site_domain_norm)
                    ):
                        soft_has_site = True
                        break
                except Exception:
                    pass

            # твиттер того же хэндла
            try:
                tw_u = try_bits.get("twitter", "") or ""
                if handle and isinstance(tw_u, str):
                    if re.search(
                        r"(?:x\.com|twitter\.com)/" + re.escape(handle) + r"(?:/|$)",
                        tw_u,
                        re.I,
                    ):
                        soft_has_handle = True
            except Exception:
                pass

            if soft_has_site or soft_has_handle:
                ok, bits = True, try_bits

            # soft-policy: агрегатор присутствует в BIO → принимаем
            if not ok and agg_norm in bio_links and try_bits:
                logger.info("Агрегатор из BIO принят по soft-policy: %s", agg_norm)
                ok, bits = True, try_bits

        if ok:
            bits = _normalize_socials(bits)

            # если офсайт подтверждён, но website пуст — проставим
            has_official_site = False
            for v in (bits or {}).values():
                try:
                    if (
                        isinstance(v, str)
                        and v
                        and site_domain_norm
                        and _host(v).endswith(site_domain_norm)
                    ):
                        has_official_site = True
                        break
                except Exception:
                    pass

            if has_official_site and not bits.get("website") and site_domain_norm:
                bits["website"] = f"https://www.{site_domain_norm}/"

            return True, bits, agg_norm

    if aggs:
        logger.info(
            "BIO: найден агрегатор(ы) %s, но подтверждение не удалось — X пропущен",
            aggs,
        )
        return False, {}, force_https(aggs[0])

    logger.info("BIO: ни офсайта, ни агрегатора — X пропущен")
    return False, {}, ""


# Кэшируем «верифицированный» выбор для домена (внутри сессии)
_VERIFIED_TW_URL: str = ""
_VERIFIED_AGG_URL: str = ""
_VERIFIED_ENRICHED: dict = {}
_VERIFIED_DOMAIN: str = ""


# Проверяем «домашний» twitter с главной сайта (и заполняем кэши)
def decide_home_twitter(
    home_twitter_url: str, site_domain: str, trust_home: bool = True
):
    global _VERIFIED_TW_URL, _VERIFIED_ENRICHED, _VERIFIED_AGG_URL, _VERIFIED_DOMAIN
    if not home_twitter_url:
        return "", {}, False, ""
    ok, extra, agg = verify_twitter_and_enrich(home_twitter_url, site_domain)
    norm = normalize_twitter_url(home_twitter_url)
    if ok:
        logger.info("X-профиль верифицирован: %s", norm)
        _VERIFIED_TW_URL = norm
        _VERIFIED_ENRICHED = dict(extra or {})
        _VERIFIED_AGG_URL = agg or ""
        _VERIFIED_DOMAIN = (site_domain or "").lower()
        return norm, (extra or {}), True, (agg or "")
    else:
        logger.info("X-профиль не подтверждён (bio/агрегатор не дал офсайт): %s", norm)
        return "", {}, False, ""


# Выбираем и подтверждаем единственный twitter для проекта
def select_verified_twitter(
    found_socials: dict,
    socials: dict,
    site_domain: str,
    brand_token: str,
    html: str,
    url: str,
    trust_home: bool = False,
) -> tuple[str, dict, str, str]:
    global _VERIFIED_TW_URL, _VERIFIED_ENRICHED, _VERIFIED_AGG_URL, _VERIFIED_DOMAIN

    # кэш на домен
    if (
        _VERIFIED_TW_URL
        and (_VERIFIED_DOMAIN or "").lower() == (site_domain or "").lower()
    ):
        return _VERIFIED_TW_URL, dict(_VERIFIED_ENRICHED), _VERIFIED_AGG_URL, ""

    twitter_final = ""
    enriched_from_agg: dict = {}
    aggregator_url = ""
    avatar_verified = ""

    # кандидаты с главной
    candidates: list[str] = []

    if (
        isinstance(socials, dict)
        and isinstance(socials.get("twitter"), str)
        and socials["twitter"]
    ):
        candidates.append(socials["twitter"])

    if (
        isinstance(found_socials, dict)
        and isinstance(found_socials.get("twitter"), str)
        and found_socials["twitter"]
    ):
        candidates.append(found_socials["twitter"])

    # добираем возможные кандидаты напрямую из HTML главной
    try:
        html_candidates = extract_twitter_profiles(html or "", url or "")
        candidates.extend(html_candidates or [])
    except Exception:
        pass

    # если вообще нет кандидатов — сразу выходим
    if not candidates:
        return "", {}, "", ""

    # dedupe + нормализация
    seen = set()
    deduped: list[str] = []
    for u in candidates or []:
        nu = normalize_twitter_url(u)
        if nu and nu not in seen:
            deduped.append(nu)
            seen.add(nu)

    # параллельная строгая проверка (если кандидатов несколько)
    if len(deduped) > 1:
        import concurrent.futures as _f

        with _f.ThreadPoolExecutor(max_workers=min(4, len(deduped))) as ex:
            futures = {
                ex.submit(verify_twitter_and_enrich, u, site_domain): u for u in deduped
            }
            for fut in _f.as_completed(futures):
                try:
                    ok, extra, agg = fut.result()
                except Exception:
                    continue
                if ok:
                    u = normalize_twitter_url(futures[fut])
                    twitter_final = u
                    enriched_from_agg = extra or {}
                    aggregator_url = agg or ""
                    _VERIFIED_TW_URL = twitter_final
                    _VERIFIED_ENRICHED = dict(enriched_from_agg)
                    _VERIFIED_AGG_URL = aggregator_url
                    _VERIFIED_DOMAIN = (site_domain or "").lower()
                    try:
                        prof = get_links_from_x_profile(twitter_final, need_avatar=True)
                        avatar_verified = (prof or {}).get("avatar", "") or ""
                    except Exception:
                        avatar_verified = ""
                    return (
                        twitter_final,
                        enriched_from_agg,
                        aggregator_url,
                        avatar_verified,
                    )
    else:
        for u in deduped:
            ok, extra, agg = verify_twitter_and_enrich(u, site_domain)
            if ok:
                twitter_final = normalize_twitter_url(u)
                enriched_from_agg = extra or {}
                aggregator_url = agg or ""
                _VERIFIED_TW_URL = twitter_final
                _VERIFIED_ENRICHED = dict(enriched_from_agg)
                _VERIFIED_AGG_URL = aggregator_url
                _VERIFIED_DOMAIN = (site_domain or "").lower()
                try:
                    prof = get_links_from_x_profile(twitter_final, need_avatar=True)
                    avatar_verified = (prof or {}).get("avatar", "") or ""
                except Exception:
                    avatar_verified = ""
                return twitter_final, enriched_from_agg, aggregator_url, avatar_verified

    logger.info(
        "X: кандидаты с сайта=%d, ни один не подтверждён — twitter пуст",
        len(deduped),
    )
    return "", {}, "", ""


# Скачиваем и сохраняем аватар X (возвращаем путь или None)
def download_twitter_avatar(
    avatar_url: str | None, twitter_url: str | None, storage_dir: str, filename: str
) -> str | None:
    if not storage_dir or not twitter_url:
        return None

    if not avatar_url:
        try:
            prof = get_links_from_x_profile(twitter_url, need_avatar=True)
            avatar_url = prof.get("avatar", "") if isinstance(prof, dict) else ""
        except Exception:
            avatar_url = ""

    if not avatar_url:
        return None

    raw = normalize_twitter_avatar(force_https(avatar_url))
    headers = {
        "User-Agent": UA,
        "Referer": twitter_url,
        "Accept": "image/avif,image/webp,image/apng,image/*;q=0.8,*/*;q=0.5",
    }
    try:
        r = requests.get(raw, timeout=25, headers=headers, allow_redirects=True)
        if (
            r.status_code == 200
            and r.content
            and "image/" in (r.headers.get("Content-Type", ""))
        ):
            import os

            os.makedirs(storage_dir, exist_ok=True)
            path = os.path.join(storage_dir, filename)
            with open(path, "wb") as f:
                f.write(r.content)
            return path
    except Exception:
        pass
    return None


# Сбрасываем кэш «верифицированного» выбора для домена (и, опционально, все кэши модулей)
def reset_verified_state(full: bool = False) -> None:
    global _VERIFIED_TW_URL, _VERIFIED_ENRICHED, _VERIFIED_AGG_URL, _VERIFIED_DOMAIN
    _VERIFIED_TW_URL = ""
    _VERIFIED_ENRICHED = {}
    _VERIFIED_AGG_URL = ""
    _VERIFIED_DOMAIN = ""
    if full:
        try:
            _PARSED_CACHE.clear()
        except Exception:
            pass
