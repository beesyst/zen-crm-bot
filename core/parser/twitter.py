from __future__ import annotations

import json
import os
import random
import re
import subprocess
from typing import Dict, List
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_setup import get_logger
from core.normalize import force_https, twitter_list_to_x, twitter_to_x
from core.parser.nitter import parse_profile
from core.settings import (
    get_http_ua,
    get_nitter_cfg,
    get_social_keys,
)

from .link_aggregator import (
    extract_socials_from_aggregator,
    find_aggregators_in_links,
    verify_aggregator_belongs,
)

logger = get_logger("twitter")
AGG_LOGGER = get_logger("link_aggregator")
UA = get_http_ua()

# Кэш уже распарсенных профилей X
_PARSED_CACHE: Dict[str, Dict] = {}
NITTER_CFG = get_nitter_cfg() or {}
NITTER_ENABLED = bool(NITTER_CFG.get("enabled", True))


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

    # если это pbs.twimg.com и размер маленький - поднимаем до 400x400
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


# Универсальный playwright-фетчер (прямой X) через playwright.js
def _run_playwright_x(u: str, timeout: int = 90) -> dict:
    host = urlparse(u).netloc.lower().replace("www.", "")
    if host not in ("x.com", "twitter.com"):
        return {}
    script = os.path.join(os.path.dirname(__file__), "playwright.js")
    try:
        SOCIAL_HOSTS = "t.co,linktr.ee,github.com,discord.com,telegram.me,medium.com,docs.google.com"

        res = subprocess.run(
            [
                "node",
                script,
                "--url",
                u,
                "--timeout",
                str(int(max(1, timeout) * 1000)),
                "--retries",
                "2",
                "--wait",
                "domcontentloaded",
                "--waitSocialHosts",
                SOCIAL_HOSTS,
                "--ua",
                UA or "",
                "--twitterProfile",
            ],
            cwd=os.path.dirname(script),
            capture_output=True,
            text=True,
            timeout=timeout + 15,
        )

    except Exception as e:
        logger.warning("playwright.js run error for %s: %s", u, e)
        return {}
    try:
        raw = (res.stdout or "").strip()
        data = json.loads(raw) if raw.startswith("{") else {}
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


# Вспомогательная функция: привести "голую" ссылку к https://
def _coerce_url(u: str) -> str:
    s = (u or "").strip()
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        return s
    if s.startswith("www."):
        return "https://" + s
    # простая эвристика: домен.tld/...
    if re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/.*)?$", s):
        return "https://" + s
    return s


# Достать все URL из произвольного текста (BIO/visible text)
def _extract_urls_from_text(text: str) -> list[str]:
    s = text or ""
    urls: list[str] = []
    # явные http/https
    for m in re.finditer(r"https?://[^\s<>\]]+", s, re.I):
        urls.append(m.group(0))
    # www. и доменные ссылки без схемы (редко, но встречается)
    for m in re.finditer(r"\b(?:www\.)[^\s<>\]]+", s, re.I):
        urls.append(m.group(0))
    for m in re.finditer(r"\b[A-Za-z0-9.-]+\.[A-Za-z]{2,}/[^\s<>\]]+", s, re.I):
        urls.append(m.group(0))
    # нормализация + дедуп
    out, seen = [], set()
    for u in urls:
        uu = force_https(_coerce_url(u)).rstrip("/")
        if uu and uu not in seen:
            out.append(uu)
            seen.add(uu)
    return out


# Парсер твитов из HTML X (fallback после Nitter)
def _extract_x_tweets_from_html(html: str, handle: str, limit: int = 5) -> List[dict]:
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
            m = re.search(r"/status/(\d+)", href)
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


# Вспомогательная функция рядом с остальными хелперами
def _expand_short_links(urls: list[str], timeout: int = 8) -> list[str]:
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
    for u in urls or []:
        try:
            u = force_https(u)
            h = _host(u)
            if h in SHORTENERS:
                r = requests.get(
                    u,
                    headers={
                        "User-Agent": UA,
                        "Referer": "https://x.com/",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    },
                    timeout=timeout,
                    allow_redirects=True,
                )
                final = force_https(r.url or u)
                # удаляем шумовые UTM-метки
                try:
                    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

                    p = urlparse(final)
                    q = [
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
                    final = (
                        urlunparse(
                            (
                                p.scheme,
                                p.netloc,
                                p.path.rstrip("/"),
                                p.params,
                                urlencode(q),
                                "",
                            )
                        )
                        or final
                    )
                except Exception:
                    pass
                out.append(final)
            else:
                out.append(u.rstrip("/"))
        except Exception:
            out.append(force_https(u).rstrip("/"))
    # Дедуп
    seen, deduped = set(), []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


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

    parsed: dict = {}

    # если nitter вкл
    if NITTER_ENABLED:
        parsed = parse_profile(safe) or {}

    # playwright
    need_pw = (not NITTER_ENABLED) or (
        not (parsed.get("links") or [])
        or (need_avatar and not (parsed.get("avatar") or "").strip())
    )

    if need_pw:
        if not NITTER_ENABLED:
            logger.info("Nitter выключен → включаем Playwright: %s", safe)
        tries = [safe]
        if need_avatar:
            tries.append(safe.rstrip("/") + "/photo")

        for try_url in tries:
            data = _run_playwright_x(try_url)

            # централизованная нормализация на Python-уровне
            if isinstance(data.get("twitter"), str):
                data["twitter"] = twitter_to_x(data.get("twitter", ""))
            if isinstance(data.get("twitter_all"), list):
                data["twitter_all"] = twitter_list_to_x(data.get("twitter_all"))

            tp = data.get("twitter_profile") or {}
            avatar_js = (tp.get("avatar") or "").strip()
            name_js = (tp.get("name") or "").strip()

            # ссылки из JS-структуры (+ бэкап и сбор из BIO/UserUrl)
            links_raw = list(tp.get("links") or [])
            if not links_raw and isinstance(data.get("links"), list):
                links_raw = list(data.get("links") or [])

            header_urls = []
            for key in ("url", "website", "user_url", "userUrl", "header_links"):
                v = tp.get(key)
                if isinstance(v, str) and v:
                    header_urls.extend(_extract_urls_from_text(v))
                elif isinstance(v, list):
                    for it in v:
                        if isinstance(it, str):
                            header_urls.extend(_extract_urls_from_text(it))

            html_profile = (tp.get("html") or "") or (data.get("html") or "")
            if html_profile:
                try:
                    for m in re.finditer(
                        r'data-testid="UserUrl"[^>]*href="([^"]+)"',
                        html_profile,
                        flags=re.I,
                    ):
                        header_urls.append(urljoin(safe, m.group(1)))
                except Exception:
                    pass

            header_urls = [
                force_https(_coerce_url(u)).rstrip("/") for u in header_urls if u
            ]
            _seen_h, _hdr = set(), []
            for u in header_urls:
                if u and u not in _seen_h:
                    _hdr.append(u)
                    _seen_h.add(u)
            header_urls = _hdr

            bio_text = (tp.get("bio") or "").strip()
            bio_urls = _extract_urls_from_text(bio_text)

            merged, seen = [], set()
            for cand in links_raw + header_urls + bio_urls:
                uu = force_https(cand).rstrip("/")
                if uu and uu not in seen:
                    merged.append(uu)
                    seen.add(uu)

            links_js = _expand_short_links(merged)

            SHORTENER_HOSTS = {
                "t.co",
                "bit.ly",
                "tinyurl.com",
                "ow.ly",
                "buff.ly",
                "t.ly",
                "shorturl.at",
            }

            def _host_only(u: str) -> str:
                try:
                    return urlparse(u).netloc.lower().replace("www.", "")
                except Exception:
                    return ""

            links_js = [u for u in links_js if _host_only(u) not in SHORTENER_HOSTS]

            if links_js or avatar_js or name_js:
                logger.info(
                    "Playwright GET+parse: %s → avatar=%s, links=%d",
                    try_url,
                    "yes" if avatar_js else "no",
                    len(links_js),
                )
                if avatar_js:
                    logger.info("Avatar URL: %s", normalize_twitter_avatar(avatar_js))
                if links_js:
                    logger.info("BIO из X: %s", links_js)

                parsed = {
                    "links": links_js,
                    "avatar": normalize_twitter_avatar(avatar_js),
                    "name": name_js,
                }
                break
            else:
                err = (data.get("timing") or {}).get("error") or ""
                fin = data.get("finalUrl") or ""
                if err or fin:
                    logger.info(
                        "[twitter] Playwright пустой ответ: %s (final=%s, error=%s)",
                        try_url,
                        fin,
                        err,
                    )
                else:
                    logger.info("[twitter] Playwright пустой ответ: %s", try_url)

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

            AGG_LOGGER.info(
                "Агрегатор %s подтвержден и спарсен: %s",
                agg_norm,
                json.dumps(bits, ensure_ascii=False),
            )
            logger.info("X подтвержден: %s", twitter_url)
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


# Выбор любого доступного инстанса Nitter из конфига
def _pick_nitter_base() -> str:
    cfg = get_nitter_cfg() or {}
    inst = cfg.get("instances") or []
    if not inst:
        return "https://nitter.net"
    return random.choice(inst)


# Подтянуть тред (реплаи того же автора) для данного статуса
def fetch_tweet_thread_via_nitter(
    handle: str, tweet_id: str, *, limit_replies: int = 12, timeout: int = 15
) -> list[dict]:
    base = _pick_nitter_base()
    url = f"{base}/{handle}/status/{tweet_id}"
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": get_http_ua()})
        if r.status_code != 200 or not r.text:
            return []
    except Exception:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    replies: list[dict] = []

    # ищем элементы ответов в основном треде
    for art in soup.select(
        "div.main-thread > div.replies > .timeline > .timeline-item"
    ):
        a_user = art.select_one("a.username")
        if not a_user:
            continue
        author = (a_user.get_text(strip=True) or "").lstrip("@").lower()
        if author != handle.lower():
            continue

        a_link = art.select_one("a[href*='/status/']")
        href = a_link.get("href", "") if a_link else ""
        m = re.search(r"/status/(\d+)", href)
        child_id = m.group(1) if m else ""
        if not child_id:
            continue

        # компактный текст ответа
        txt = re.sub(r"\s+", " ", art.get_text(" ", strip=True)).strip()
        if len(txt) > 800:
            txt = txt[:800] + "…"

        replies.append(
            {
                "id": child_id,
                "text": txt,
                "status_url": (
                    force_https(urljoin(base, href))
                    if href
                    else f"https://x.com/{handle}/status/{child_id}"
                ),
            }
        )
        if len(replies) >= limit_replies:
            break

    return replies


# Возврат {"videos": [..], "images": [..]} из страницы статуса Nitter
def fetch_tweet_media_via_nitter(
    handle: str,
    tweet_id: str,
    *,
    timeout: int = 15,
    base: str | None = None,
) -> dict:
    base = (base or "").strip() or _pick_nitter_base()
    url = f"{base}/{handle}/status/{tweet_id}"
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": get_http_ua()})
        if r.status_code != 200 or not r.text:
            return {"videos": [], "images": []}
    except Exception:
        return {"videos": [], "images": []}

    soup = BeautifulSoup(r.text, "html.parser")
    videos, images = [], []

    # видео: data-url/src на <video>, <source src>, а также ссылки вида /video/...
    for vc in soup.select(
        "div.video-container, div.attachment.video-container, div.gallery-video div.attachment.video-container"
    ):
        v = vc.select_one("video[data-url], video[src]")
        if v:
            m3u8 = (v.get("data-url") or v.get("src") or "").strip()
            if m3u8:
                videos.append(urljoin(base, m3u8))
            poster = (v.get("poster") or "").strip()
            if poster:
                images.append(urljoin(base, poster))
        for s in vc.select("source[src]"):
            m3 = (s.get("src") or "").strip()
            if m3:
                videos.append(urljoin(base, m3))

    for a in soup.select("a[href*='/video/']"):
        href = (a.get("href") or "").strip()
        if href:
            videos.append(urljoin(base, href))

    # картинки: <img src> + still-image (/pic/…)
    for img in soup.select("div.attachments img[src], div.gallery-row img[src]"):
        src = (img.get("src") or "").strip()
        if src:
            images.append(urljoin(base, src))

    for a in soup.select("a.still-image[href], a[href^='/pic/']"):
        href = (a.get("href") or "").strip()
        if href:
            images.append(urljoin(base, href))

    # OpenGraph фолбэки
    og_img = soup.select_one(
        "meta[property='og:image'][content], meta[name='og:image'][content]"
    )
    if og_img:
        images.append((og_img.get("content") or "").strip())
    og_vid = soup.select_one(
        "meta[property='og:video'][content], meta[name='og:video'][content]"
    )
    if og_vid:
        videos.append((og_vid.get("content") or "").strip())

    def _dedup(xs):
        seen, out = set(), []
        for u in xs:
            uu = force_https(u).rstrip("/")
            if uu and uu not in seen:
                seen.add(uu)
                out.append(uu)
        return out

    return {"videos": _dedup(videos), "images": _dedup(images)}


# Возврат списка URL для attachments: сперва прямой m3u8 (если есть), затем постер (картинка)
def get_tweet_attachments(
    handle: str,
    tweet_id: str,
    *,
    timeout: int = 15,
    base: str | None = None,
) -> list[str]:
    media = (
        fetch_tweet_media_via_nitter(handle, tweet_id, timeout=timeout, base=base) or {}
    )
    videos = list(media.get("videos") or [])
    images = list(media.get("images") or [])

    def _decode_nitter_video(u: str) -> str:
        # nitter: /video/<token>/<ENCODED_HTTPS_URL> → https://video.twimg.com/...m3u8
        try:
            p = urlparse(u)
            if "/video/" in (p.path or ""):
                encoded = u.split("/video/", 1)[1].split("/", 1)[1]
                decoded = unquote(encoded).replace("&amp;", "&")
                return force_https(decoded).rstrip("/")
        except Exception:
            pass
        return force_https(u).rstrip("/")

    def _decode_nitter_pic(u: str) -> str:
        # nitter: /pic/<encoded> → https://pbs.twimg.com/...
        try:
            p = urlparse(u)
            if "/pic/" in (p.path or ""):
                s = p.path.split("/pic/", 1)[1]
                s = unquote(s)
                if s.startswith("//"):
                    s = "https:" + s
                elif s.startswith("http://"):
                    s = "https://" + s[7:]
                elif not s.startswith("https://"):
                    s = "https://" + s.lstrip("/")
                return force_https(s).rstrip("/")
        except Exception:
            pass
        return force_https(u).rstrip("/")

    out: list[str] = []

    # видео первым элементом
    for v in videos:
        out.append(_decode_nitter_video(v))

    # постер/картинка как второй элемент (если есть)
    if images:
        out.append(_decode_nitter_pic(images[0]))

    # дедуп
    seen, deduped = set(), []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


# Для каждого родительского твита подтягиваем тред; не влияет на handle_limit
def _enrich_with_threads(items: list[dict], *, thread_limit: int = 12) -> list[dict]:
    out = []
    for it in items or []:
        handle = it.get("handle", "")
        tid = it.get("id", "")
        if not handle or not tid:
            out.append(it)
            continue
        try:
            thread = (
                fetch_tweet_thread_via_nitter(handle, tid, limit_replies=thread_limit)
                or []
            )
        except Exception:
            thread = []
        new_it = dict(it)
        new_it["thread"] = thread
        out.append(new_it)
    return out


# Публичная обертка для получения твитов (через Nitter)
def get_recent_tweets(
    handles: list[str], handle_limit: int = 5, oldest_days: int | None = None
) -> list[dict]:
    from core.parser.nitter import fetch_tweets as _nitter_fetch

    out = []
    per_handle_limit = max(1, int(handle_limit or 5))

    for h in handles or []:
        items = []
        # 1) пробуем Nitter
        try:
            items = (
                _nitter_fetch(h, limit=per_handle_limit, oldest_days=oldest_days) or []
            )
        except Exception:
            items = []

        # нормализация твитов от Nitter: гарантируем handle, id, status_url
        normed = []
        for it in items or []:
            it2 = dict(it)
            # handle: если не пришел - проставим текущий
            it2["handle"] = (it.get("handle") or h).strip()

            # id: пытаемся взять как есть либо выдрать из status_url/url
            tid = (it.get("id") or "").strip()
            if not tid:
                import re

                for k in ("status_url", "url"):
                    u = (it.get(k) or "").strip()
                    m = re.search(r"/status/(\d+)", u)
                    if m:
                        tid = m.group(1)
                        break
            if not tid:
                # без id медиа не подтянуть - пропускаем такой элемент
                continue
            it2["id"] = tid

            # status_url: строим, если нет
            if not (it.get("status_url") or "").strip():
                it2["status_url"] = f"https://x.com/{it2['handle']}/status/{tid}"

            normed.append(it2)

        items = normed

        # fallback на X (если Nitter пуст)
        if not items:
            try:
                # один фолбек-вызов, усиленные ожидания внутри _run_playwright_x
                data = _run_playwright_x(f"https://x.com/{h}", timeout=90) or {}
                html = (
                    (data.get("twitter_profile") or {}).get("html")
                    or data.get("html")
                    or ""
                )
                if html:
                    items = (
                        _extract_x_tweets_from_html(html, h, limit=per_handle_limit)
                        or []
                    )
                    logger.info(
                        "Playwright GET+parse: https://x.com/%s → tweets=%d",
                        h,
                        len(items),
                    )
                else:
                    fin = data.get("finalUrl") or ""
                    err = (data.get("timing") or {}).get("error") or ""
                    logger.info(
                        "[twitter] Playwright пустой ответ: https://x.com/%s (final=%s, error=%s)",
                        h,
                        fin,
                        err,
                    )
            except Exception as e:
                logger.warning(
                    "playwright.js parse error for https://x.com/%s: %s", h, e
                )

        out.extend(items or [])

    # сортировка по времени, если есть datetime
    try:
        import datetime as _dt

        def _key(it):
            dt = (it or {}).get("datetime") or ""
            try:
                return _dt.datetime.fromisoformat(dt.replace("Z", "+00:00"))
            except Exception:
                return _dt.datetime.min

        out.sort(key=_key, reverse=True)
    except Exception:
        pass

    # легкий дедуп по (handle,id)
    seen = set()
    deduped = []
    for it in out:
        k = (it.get("handle", ""), it.get("id", ""))
        if k not in seen:
            seen.add(k)
            deduped.append(it)

    # тянем тред (реплаи автора) для каждого твита
    try:
        deduped = _enrich_with_threads(deduped, thread_limit=12)
    except Exception:
        pass

    # тянем медиа со страницы статуса только если их нет после первичного парсинга
    try:
        enriched = []
        for it in deduped:
            h = (it.get("handle") or "").strip()
            tid = (it.get("id") or "").strip()
            base = (it.get("nitter_base") or "").strip()
            new_it = dict(it)

            has_inline = bool(
                new_it.get("attachments")
                or new_it.get("videos")
                or new_it.get("images")
            )
            if h and tid and not has_inline:
                media = fetch_tweet_media_via_nitter(h, tid, base=base) or {}
                vids = list(media.get("videos") or [])
                imgs = list(media.get("images") or [])
                atts = get_tweet_attachments(h, tid, base=base) or []
                mm = list(dict.fromkeys((new_it.get("media") or []) + imgs + vids))
                new_it["media"] = mm
                new_it["videos"] = vids
                new_it["attachments"] = atts

            # если уже есть - просто нормализуем уникальность
            else:
                imgs = list(new_it.get("images") or new_it.get("media") or [])
                vids = list(new_it.get("videos") or [])
                atts = list(new_it.get("attachments") or [])

                # лёгкая дедуп/нормализация вывода
                def _uniq(xs):
                    seen, out = set(), []
                    for u in xs:
                        uu = force_https(u).rstrip("/")
                        if uu and uu not in seen:
                            seen.add(uu)
                            out.append(uu)
                    return out

                new_it["images"] = _uniq(imgs)
                new_it["media"] = _uniq(imgs + vids)
                new_it["videos"] = _uniq(vids)
                new_it["attachments"] = _uniq(atts)

            enriched.append(new_it)
        deduped = enriched
    except Exception:
        pass

    return deduped
