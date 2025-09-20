from __future__ import annotations

import json
import os
import re
import subprocess
from time import time as now
from typing import Dict, List, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_setup import get_logger
from core.normalize import force_https
from core.settings import get_nitter_cfg

from .link_aggregator import (
    extract_socials_from_aggregator,
    find_aggregators_in_links,
    verify_aggregator_belongs,
)

logger = get_logger("twitter")


# Считывание конфига Nitter (включение/инстансы/таймауты/TTL бана)
def _load_nitter_cfg() -> dict:
    cfg = get_nitter_cfg()
    if not isinstance(cfg, dict):
        raise RuntimeError("settings.yml: parser.nitter должен быть словарем")

    required = [
        "enabled",
        "instances",
        "retry_per_instance",
        "timeout_sec",
        "bad_ttl_sec",
    ]
    missing = [k for k in required if k not in cfg]
    if missing:
        raise RuntimeError(
            f"settings.yml: отсутствуют ключи parser.nitter: {', '.join(missing)}"
        )

    if not isinstance(cfg["instances"], list) or not cfg["instances"]:
        raise RuntimeError(
            "settings.yml: parser.nitter.instances должен быть непустым списком"
        )

    instances = [
        force_https(str(x)).rstrip("/") for x in cfg["instances"] if str(x).strip()
    ]
    if not instances:
        raise RuntimeError(
            "settings.yml: parser.nitter.instances после нормализации пуст"
        )

    return {
        "enabled": bool(cfg["enabled"]),
        "instances": instances,
        "retry_per_instance": int(cfg["retry_per_instance"]),
        "timeout_sec": int(cfg["timeout_sec"]),
        "bad_ttl_sec": int(cfg["bad_ttl_sec"]),
        "use_stealth": bool(cfg.get("use_stealth", True)),
        "max_instances_try": int(cfg.get("max_instances_try", max(3, len(instances)))),
    }


_NITTER = _load_nitter_cfg()
_NITTER_HTML_CACHE: Dict[str, Tuple[str, str]] = {}
_NITTER_BAD: Dict[str, float] = {}
_PARSED_CACHE: Dict[str, Dict] = {}


# Хелпер: домен из URL без www
def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# Нормализация ссылки X/Twitter к виду https://x.com/<handle>
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


# Нормализация URL аватара X (мусор, раскодируем nitter /pic/)
def normalize_twitter_avatar(url: str | None) -> str:
    u = force_https(url or "")
    if not u:
        return ""

    # nitter absolute or relative: https://nitter.net/pic/... или /pic/...
    try:
        p = urlparse(u)
        if "/pic/" in (p.path or ""):
            # отбросим хост, декодировать по существующей функции
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


# Декодер nitter /pic/<encoded> в прямой https-ссылку
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


# Получение списка живых инстансов nitter (без забаненных на TTL)
def _alive_instances() -> List[str]:
    t = now()
    alive = []
    for inst in _NITTER["instances"]:
        inst = force_https(inst.rstrip("/"))
        if _NITTER_BAD.get(inst, 0) <= t:
            alive.append(inst)
    return alive


# Бан инстанса nitter на bad_ttl_sec
def _ban(inst: str):
    _NITTER_BAD[force_https(inst.rstrip("/"))] = now() + max(60, _NITTER["bad_ttl_sec"])


# Парс HTML профиля через headless-скрипт (browser_fetch.js) с nitter
def _fetch_nitter_html(handle: str) -> Tuple[str, str]:
    if not handle:
        return "", ""

    def _cache_key(inst: str) -> str:
        return f"{force_https(inst).rstrip('/')}|{handle.lower()}"

    script = os.path.join(os.path.dirname(__file__), "browser_fetch.js")

    # лёгкая попытка: обычный GET к nitter-инстансу
    def _http_try(inst_url: str) -> tuple[str, int]:
        try:
            u = f"{force_https(inst_url).rstrip('/')}/{handle}"
            r = requests.get(
                u,
                timeout=int(_NITTER.get("timeout_sec", 14)) or 14,
                headers={"User-Agent": "Mozilla/5.0"},
                allow_redirects=True,
            )
            html = (r.text or "").strip()
            return html, r.status_code
        except Exception:
            return "", 0

    # хард попытка: headless-запрос через browser_fetch.js (со stealth/fingerprint)
    def _run(inst_url: str):
        try:
            u = f"{force_https(inst_url).rstrip('/')}/{handle}"
            # --raw включает html+text; positional URL поддерживается parseArgs
            args = ["node", script, u, "--raw"]
            return subprocess.run(
                args,
                cwd=os.path.dirname(script),
                capture_output=True,
                text=True,
                timeout=max(int(_NITTER.get("timeout_sec", 12)) + 12, 30),
            )
        except Exception as e:
            logger.warning("Nitter runner failed %s: %s", inst_url, e)
            return None

    last_err = None

    first_pass = (
        ("headless", "http")
        if _NITTER.get("use_stealth", True)
        else ("http", "headless")
    )
    max_try = max(1, int(_NITTER.get("max_instances_try", 3)))

    for mode in first_pass:
        tried = 0
        # каждый круг берем актуальный список живых, чтобы исключить свежезабаненных
        for inst in _alive_instances():
            if tried >= max_try:
                break
            key = _cache_key(inst)
            if key in _NITTER_HTML_CACHE:
                return _NITTER_HTML_CACHE[key]

            if mode == "headless":
                head_retries = max(1, int(_NITTER.get("retry_per_instance", 1)))
                for _ in range(head_retries):
                    res = _run(inst)
                    if not res:
                        _ban(inst)
                        last_err = "runner_failed"
                        break
                    try:
                        data = (
                            json.loads(res.stdout)
                            if (res.stdout or "").strip().startswith("{")
                            else {}
                        )
                    except Exception:
                        data = {}
                    html = (
                        (data.get("html") or "").strip()
                        if isinstance(data, dict)
                        else ""
                    )
                    status = int(data.get("status", 0) or 0)

                    if html and _html_matches_handle(html, handle):
                        _NITTER_HTML_CACHE[key] = (html, force_https(inst).rstrip("/"))
                        return html, force_https(inst).rstrip("/")

                    kind = (data.get("antiBot") or {}).get("kind", "")
                    if (
                        kind
                        or status in (0, 403, 429, 503)
                        or (status == 200 and not _html_matches_handle(html, handle))
                    ):
                        _ban(inst)
                    last_err = kind or f"HEADLESS HTTP {status}" or "no_html"

                tried += 1
                continue

            # mode == http
            http_retries = max(1, int(_NITTER.get("retry_per_instance", 1)))
            ok_html, code = "", 0
            for _ in range(http_retries):
                ok_html, code = _http_try(inst)
                if ok_html and _html_matches_handle(ok_html, handle):
                    _NITTER_HTML_CACHE[key] = (ok_html, force_https(inst).rstrip("/"))
                    return ok_html, force_https(inst).rstrip("/")

            if code in (0, 403, 429, 503) or (
                code == 200 and not _html_matches_handle(ok_html, handle)
            ):
                _ban(inst)
            else:
                logger.warning(
                    "Nitter inst %s вернул %s без валидного HTML для @%s, не баним",
                    inst,
                    code,
                    handle,
                )
            last_err = f"HTTP {code or 0}"
            tried += 1

    if last_err:
        logger.warning("Nitter: all instances failed (%s)", last_err)
        logger.warning("Nitter HTML пуст для %s", handle)
    return "", ""


# Извлечение ссылки на аватар из HTML nitter
def _pick_avatar_from_soup(soup: BeautifulSoup, inst_base: str) -> tuple[str, str]:
    a = soup.select_one(".profile-card a.profile-card-avatar[href]")
    if a and a.get("href"):
        href = (a.get("href") or "").strip()
        raw = (
            f"{force_https(inst_base).rstrip('/')}{href}"
            if href.startswith("/")
            else (
                href
                if href.startswith("http")
                else f"{force_https(inst_base).rstrip('/')}/{href.lstrip('/')}"
            )
        )
        normalized = _decode_nitter_pic_url(href)
        return raw, normalized

    img = soup.select_one(
        ".profile-card a.profile-card-avatar img, "
        "a.profile-card-avatar img, "
        ".profile-card img.avatar, "
        "img[src*='pbs.twimg.com/profile_images/']"
    )
    if img and img.get("src"):
        src = (img.get("src") or "").strip()
        raw = (
            f"{force_https(inst_base).rstrip('/')}{src}"
            if src.startswith("/")
            else (
                src
                if src.startswith("http")
                else f"{force_https(inst_base).rstrip('/')}/{src.lstrip('/')}"
            )
        )
        normalized = _decode_nitter_pic_url(src)
        return raw, normalized

    meta = soup.select_one("meta[property='og:image'], meta[name='og:image']")
    if meta and meta.get("content"):
        c = (meta["content"] or "").strip()
        if "/pic/" in c or "%2F" in c or "%3A" in c:
            raw = (
                f"{force_https(inst_base).rstrip('/')}{c}"
                if c.startswith("/")
                else (
                    c
                    if c.startswith("http")
                    else f"{force_https(inst_base).rstrip('/')}/{c.lstrip('/')}"
                )
            )
            normalized = _decode_nitter_pic_url(c)
            return raw, normalized
        if "pbs.twimg.com" in c:
            return force_https(c), force_https(c)
    return "", ""


# Парс профиля через nitter: имя, ссылки из био, аватар
def _parse_nitter_profile(twitter_url: str) -> Dict[str, object]:
    if not _NITTER["enabled"]:
        return {}

    m = re.match(
        r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$",
        (twitter_url or "") + "/",
        re.I,
    )
    handle = m.group(1) if m else ""
    if not handle:
        return {}

    html, inst = _fetch_nitter_html(handle)
    if not html or not inst:
        logger.warning("Nitter HTML пуст для %s", handle)
        return {}

    soup = BeautifulSoup(html, "html.parser")
    name_tag = soup.select_one(".profile-card-fullname")
    name = (name_tag.get_text(strip=True) if name_tag else "") or ""

    base = f"{(inst or '').rstrip('/')}/{handle}"

    links = set()
    seen = set()
    areas = [
        ".profile-card .profile-website a",
        ".profile-card .profile-bio a",
        ".profile-website a",
        ".profile-bio a",
    ]
    for sel in areas:
        for a in soup.select(sel):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            try:
                abs_u = urljoin(base, href)
            except Exception:
                abs_u = href
            if not abs_u.startswith("http"):
                continue
            u = force_https(abs_u)
            if u not in seen:
                links.add(u)
                seen.add(u)

    # аватар: raw для лога (инстанс/pic/...), normalized для использования
    avatar_raw, avatar_norm = _pick_avatar_from_soup(soup, inst)
    logger.info(
        "Nitter GET+parse: %s/%s → avatar=%s, links=%d",
        (inst or "-").rstrip("/"),
        handle,
        "yes" if avatar_raw or avatar_norm else "no",
        len(links),
    )

    return {
        "links": list(links),
        "avatar": normalize_twitter_avatar(avatar_norm or ""),
        "avatar_raw": force_https(avatar_raw or ""),
        "name": name,
    }


# Запуск node/twitter_scraper.js (Playwright) как запасной вариант
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
        data = (
            json.loads(res.stdout) if (res.stdout or "").strip().startswith("{") else {}
        )
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


# Профиль X: {links, avatar, name} (nitter → playwright-фолбэк)
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
    parsed = _parse_nitter_profile(safe) or {}

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

    avatar_raw = (parsed.get("avatar_raw") or "").strip()
    if avatar_raw:
        # если аватар пришел из nitter - логируем сырой url вида https://<inst>/pic/...
        logger.info("Avatar URL: %s", avatar_raw)
    elif (out.get("avatar") or "").strip():
        # если пришел напрямую из x.com - логируем pbs.twimg.com-линк
        logger.info("Avatar URL: %s", out["avatar"])

    _PARSED_CACHE[safe] = out
    return out


# Детектор валидного профиля в Nitter
def _html_matches_handle(html: str, handle: str) -> bool:
    if not html or not handle:
        return False
    low = html.lower()
    h = handle.lower()

    # типичный якорь профиля в nitter: href="/<handle>"
    if re.search(rf'href\s*=\s*["\']/\s*{re.escape(h)}(?:["\'/?# ]|$)', low):
        return True
    # username в карточке: @<handle>
    if re.search(rf"@{re.escape(h)}(?:[\"\' <]|$)", low):
        return True
    # подстраховка: есть блоки profile-card и упоминание handle
    if ("profile-card" in low) and (h in low):
        return True
    # на очень короткий html не ведемся
    if len(low) < 600:
        return False

    return False


# Вытаскивание всех кандидатов-профилей X из HTML (ссылки + голый текст)
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


# Быстрая проверка, что профиль живой/реальный
def _is_valid_profile(parsed: dict) -> bool:
    if not isinstance(parsed, dict):
        return False
    links = parsed.get("links") or []
    return len(links) >= 1


# Проверка твиттера и попытка домержить соцсети через агрегатор из BIO
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
    logger.info("BIO из Nitter: %s", bio_links)

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

    # если X подтверждён по сайту или по агрегатору - лог один раз и возвращаем
    if confirmed_by_site or enriched_bits:
        if confirmed_by_site and site_domain_norm and not enriched_bits.get("website"):
            enriched_bits["website"] = f"https://{site_domain_norm}/".replace(
                "//www.", "//"
            )
        logger.info("X подтвержден: %s", twitter_url)
        return True, enriched_bits, agg_used

    def _normalize_socials(d: dict) -> dict:
        out = {}
        for k, v in (d or {}).items():
            if not isinstance(v, str) or not v:
                continue
            vv = force_https(v)
            if k == "twitter":
                vv = vv.replace("twitter.com", "x.com")
            if k in (
                "website",
                "document",
                "twitter",
                "discord",
                "telegram",
                "youtube",
                "linkedin",
                "reddit",
                "medium",
                "github",
            ):
                out[k] = vv
        return out

    # Проверяем каждый агрегатор: жёстко → мягко → soft-policy из bio
    for agg in aggs:
        agg_norm = force_https(agg)

        # жесткая проверка принадлежности
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

            # soft-policy: агрегатор присутствует в bio → принимаем
            if not ok and agg_norm in bio_links and try_bits:
                logger.info("Агрегатор из BIO принят по soft-policy: %s", agg_norm)
                ok, bits = True, try_bits

        if ok:
            bits = _normalize_socials(bits)

            # если офсайт подтвержден, но website пуст - проставим
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
            "BIO: найден агрегатор(ы) %s, но подтверждение не удалось - X пропущен",
            aggs,
        )
        return False, {}, force_https(aggs[0])

    logger.info("BIO: ни офсайта, ни агрегатора - X пропущен")
    return False, {}, ""


# Глобальное «кэш-решение» для одного домена (внутрисессионно)
_VERIFIED_TW_URL: str = ""
_VERIFIED_AGG_URL: str = ""
_VERIFIED_ENRICHED: dict = {}
_VERIFIED_DOMAIN: str = ""


# Проверка домашнего twitter с главной сайта (и кэши)
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
        logger.info("X-профиль не подтвержден (bio/агрегатор не дал офсайт): %s", norm)
        return "", {}, False, ""


# Выбор и подтверждение единственного twitter для проекта
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

    # если вообще нет кандидатов, сразу выходим
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
        "X: кандидаты с сайта=%d, ни один не подтвержден - twitter пуст",
        len(deduped),
    )
    return "", {}, "", ""


# Загрузка и сохранение аватара X (возвращаем путь или None)
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
        "User-Agent": "Mozilla/5.0",
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


# Сброс кэша "верифицированного" выбора для домена
def reset_verified_state(full: bool = False) -> None:
    global _VERIFIED_TW_URL, _VERIFIED_ENRICHED, _VERIFIED_AGG_URL, _VERIFIED_DOMAIN
    _VERIFIED_TW_URL = ""
    _VERIFIED_ENRICHED = {}
    _VERIFIED_AGG_URL = ""
    _VERIFIED_DOMAIN = ""
    if full:
        try:
            _PARSED_CACHE.clear()
            _NITTER_HTML_CACHE.clear()
            _NITTER_BAD.clear()
        except Exception:
            pass
