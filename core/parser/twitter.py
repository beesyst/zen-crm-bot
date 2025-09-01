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

logger = get_logger("parser.twitter")


# Загрузка settings.yml (локально, без падений)
def _load_settings_yaml() -> dict:
    try:
        from pathlib import Path

        import yaml
        from core.paths import CONFIG_DIR

        p = Path(CONFIG_DIR) / "settings.yml"
        if not p.exists():
            return {}
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


# Считывание конфгиа Nitter (включение/инстансы/таймауты/TTL бана)
def _load_nitter_cfg() -> dict:
    s = _load_settings_yaml()
    n = ((s.get("parser") or {}).get("nitter")) or {}
    return {
        "enabled": bool(n.get("enabled", True)),
        "instances": [
            force_https(str(x)).rstrip("/")
            for x in (n.get("instances") or ["https://nitter.net"])
        ],
        "retry_per_instance": int(n.get("retry_per_instance", 2)),
        "timeout_sec": int(n.get("timeout_sec", 20)),
        "bad_ttl_sec": int(n.get("bad_ttl_sec", 300)),
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
    s = force_https(u.strip())
    s = re.sub(r"^https://twitter\.com", "https://x.com", s, flags=re.I)
    s = re.sub(r"[?#].*$", "", s)
    s = re.sub(
        r"/(photo|media|with_replies|likes|lists|following|followers)/?$",
        "",
        s,
        flags=re.I,
    )
    m = re.match(r"^https://x\.com/([A-Za-z0-9_]{1,15})/?$", s, re.I)
    return f"https://x.com/{m.group(1)}" if m else s.rstrip("/")


# Нормализация URL аватара X (мусор, раскодируем nitter /pic/)
def normalize_twitter_avatar(url: str | None) -> str:
    u = force_https(url or "")
    if not u:
        return ""
    if u.startswith("/pic/"):
        u = _decode_nitter_pic_url(u)
    if u.startswith("pbs.twimg.com/"):
        u = "https://" + u
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


# Поулчение списка живых инстансов nitter (без забаненных на TTL)
def _alive_instances() -> List[str]:
    t = now()
    alive = []
    for inst in _NITTER["instances"] or []:
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
    key = handle.lower()
    if key in _NITTER_HTML_CACHE:
        return _NITTER_HTML_CACHE[key]

    script = os.path.join(os.path.dirname(__file__), "browser_fetch.js")
    last_err = None

    def _run(inst_url: str):
        try:
            u = f"{inst_url.rstrip('/')}/{handle}"
            return subprocess.run(
                ["node", script, u, "--raw"],
                cwd=os.path.dirname(script),
                capture_output=True,
                text=True,
                timeout=max(_NITTER["timeout_sec"] + 6, 20),
            )
        except Exception as e:
            logger.warning("Nitter runner failed %s: %s", inst_url, e)
            return None

    for inst in _alive_instances():
        for _ in range(max(1, _NITTER["retry_per_instance"])):
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
            html = (data.get("html") or "") if isinstance(data, dict) else ""
            if html.strip():
                _NITTER_HTML_CACHE[key] = (html, inst)
                return html, inst
            kind = (data.get("antiBot") or {}).get("kind", "")
            status = data.get("status", 0)
            if kind or status in (403, 429, 503, 0):
                _ban(inst)
            last_err = kind or f"HTTP {status}" or "no_html"

    if last_err:
        logger.warning("Nitter: all instances failed (%s)", last_err)
    return "", ""


# Извлечение ссылки на аватар из HTML nitter
def _pick_avatar_from_soup(soup: BeautifulSoup) -> str:
    img = soup.select_one(
        ".profile-card a.profile-card-avatar img, "
        "a.profile-card-avatar img, "
        ".profile-card img.avatar, "
        "img[src*='pbs.twimg.com/profile_images/']"
    )
    if img and img.get("src"):
        return _decode_nitter_pic_url(img["src"])

    a = soup.select_one(".profile-card a.profile-card-avatar[href]")
    if a and a.get("href"):
        return _decode_nitter_pic_url(a["href"])

    meta = soup.select_one("meta[property='og:image'], meta[name='og:image']")
    if meta and meta.get("content"):
        c = meta["content"]
        if "/pic/" in c or "%2F" in c or "%3A" in c:
            return _decode_nitter_pic_url(c)
        if "pbs.twimg.com" in c:
            return force_https(c)
    return ""


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
    if not html:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    name_tag = soup.select_one(".profile-card .profile-name-full")
    name = (name_tag.get_text(strip=True) if name_tag else "") or ""

    links = set()
    for a in soup.select(".profile-bio a, .profile-website a"):
        href = a.get("href", "") or ""
        if href.startswith("/url/"):
            href = href[len("/url/") :]
        if href.startswith("/") or not href:
            continue
        if href.startswith("http"):
            links.add(force_https(href))

    avatar = _pick_avatar_from_soup(soup)
    return {
        "links": list(links),
        "avatar": normalize_twitter_avatar(avatar),
        "name": name,
    }


# Запуск node/twitter_scraper.js (Playwright) как запасной вариант
def _run_playwright(u: str, timeout: int = 90) -> dict:
    script = os.path.join(os.path.dirname(__file__), "twitter_scraper.js")
    try:
        res = subprocess.run(
            ["node", script, u],
            cwd=os.path.dirname(script),
            capture_output=True,
            text=True,
            timeout=timeout,
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

    parsed = _parse_nitter_profile(safe) or {}
    if not parsed.get("avatar") and need_avatar:
        tries = [safe, safe.rstrip("/") + "/photo"]
        for u in tries:
            data = _run_playwright(u)
            if data.get("links") or data.get("avatar") or data.get("name"):
                parsed = {
                    "links": parsed.get("links") or data.get("links") or [],
                    "avatar": normalize_twitter_avatar(
                        data.get("avatar") or parsed.get("avatar") or ""
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


# Вытаскивание всех кандидатов-профилей X из HTML (ссылки + голый текст)
def extract_twitter_profiles(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    profiles = set()

    for a in soup.find_all("a", href=True):
        raw = urljoin(base_url, a["href"])
        if not re.search(r"(twitter\.com|x\.com)", raw, re.I):
            continue
        if re.search(r"/status/|/share|/intent|/search|/hashtag/", raw, re.I):
            continue
        try:
            p = urlparse(raw)
            clean = f"{p.scheme}://{p.netloc}{p.path}"
            clean = clean.replace("twitter.com", "x.com")
            clean = force_https(clean.rstrip("/"))
        except Exception:
            continue
        m = re.match(r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})$", clean, re.I)
        if m:
            profiles.add(clean)

    text = html or ""
    for m in re.finditer(
        r"https?://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_/])",
        text,
        re.I,
    ):
        try:
            u = m.group(0)
            u = u.replace("twitter.com", "x.com")
            u = force_https(u.rstrip("/"))
            if re.match(r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})$", u, re.I):
                profiles.add(u)
        except Exception:
            pass

    return list(profiles)


# Быстрая проверка, что профиль живой/реальный
def _is_valid_profile(parsed: dict) -> bool:
    if not isinstance(parsed, dict):
        return False
    name = (parsed.get("name") or "").strip().lower()
    avatar = (parsed.get("avatar") or "").strip()
    links = parsed.get("links") or []
    if (not avatar) and (not name or "new to x" in name) and (not links):
        return False
    return True


# Проверка твиттера и попытка домержить соцсети через агрегатор из BIO
def verify_twitter_and_enrich(
    twitter_url: str, site_domain: str
) -> tuple[bool, dict, str]:
    data = get_links_from_x_profile(twitter_url, need_avatar=False)
    if not _is_valid_profile(data):
        return False, {}, ""

    from .link_aggregator import (
        extract_socials_from_aggregator,
        find_aggregators_in_links,
        verify_aggregator_belongs,
    )

    aggs = find_aggregators_in_links(data.get("links") or [])
    handle = ""
    m = re.match(
        r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$",
        (twitter_url or "") + "/",
        re.I,
    )
    if m:
        handle = m.group(1)

    for agg in aggs:
        ok, _bits = verify_aggregator_belongs(agg, site_domain, handle)
        if ok:
            socials_clean = extract_socials_from_aggregator(agg) or {}
            if site_domain:
                socials_clean["websiteURL"] = f"https://www.{site_domain}/"
            return True, socials_clean, agg

    for b in data.get("links") or []:
        try:
            if _host(b).endswith(site_domain):
                return True, {"websiteURL": f"https://www.{site_domain}/"}, ""
        except Exception:
            pass

    return True, {}, ""


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
        _VERIFIED_TW_URL = norm
        _VERIFIED_ENRICHED = dict(extra or {})
        _VERIFIED_AGG_URL = agg or ""
        _VERIFIED_DOMAIN = (site_domain or "").lower()
        return norm, (extra or {}), True, (agg or "")
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

    if (
        _VERIFIED_TW_URL
        and (_VERIFIED_DOMAIN or "").lower() == (site_domain or "").lower()
    ):
        return _VERIFIED_TW_URL, dict(_VERIFIED_ENRICHED), _VERIFIED_AGG_URL, ""

    twitter_final = ""
    enriched_from_agg = {}
    aggregator_url = ""
    avatar_verified = ""

    if found_socials.get("twitterURL"):
        t_final, t_extra, _ok, agg_url = decide_home_twitter(
            found_socials["twitterURL"], site_domain, trust_home
        )
        if t_final:
            _VERIFIED_TW_URL = twitter_final = normalize_twitter_url(t_final)
            _VERIFIED_ENRICHED = dict(t_extra or {})
            _VERIFIED_AGG_URL = aggregator_url = agg_url or ""
            _VERIFIED_DOMAIN = (site_domain or "").lower()
            try:
                prof = get_links_from_x_profile(twitter_final, need_avatar=True)
                avatar_verified = (prof or {}).get("avatar", "") or ""
            except Exception:
                avatar_verified = ""
            return twitter_final, dict(t_extra or {}), aggregator_url, avatar_verified

    browser_twitter_ordered = []
    if isinstance(socials, dict) and isinstance(socials.get("twitterAll"), list):
        browser_twitter_ordered = [
            u for u in socials["twitterAll"] if isinstance(u, str) and u
        ]

    candidates = list(browser_twitter_ordered)

    def _extract_from(html_text: str, base: str) -> list[str]:
        try:
            return extract_twitter_profiles(html_text, base)
        except Exception:
            return []

    candidates.extend(_extract_from(html, url))

    docs = socials.get("documentURL") or found_socials.get("documentURL") or ""
    docs_html = ""
    if docs:
        try:
            r = requests.get(
                force_https(docs), timeout=20, headers={"User-Agent": "Mozilla/5.0"}
            )
            docs_html = r.text or ""
            candidates.extend(_extract_from(docs_html, docs))
        except Exception:
            pass

    seen = set()
    deduped: list[str] = []
    for u in candidates:
        nu = normalize_twitter_url(u)
        if nu and nu not in seen:
            deduped.append(nu)
            seen.add(nu)

    dom_set = {normalize_twitter_url(u) for u in browser_twitter_ordered}

    def _handle(u: str) -> str:
        m = re.match(
            r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$", (u or "") + "/", re.I
        )
        return (m.group(1) if m else "").lower()

    bt = (brand_token or "").lower()

    for u in deduped:
        if u in dom_set:
            h = _handle(u)
            logger.info("X from site: %s (handle=%s, brand=%s)", u, h, bt)
            _VERIFIED_TW_URL = twitter_final = u
            _VERIFIED_ENRICHED = {}
            _VERIFIED_AGG_URL = ""
            _VERIFIED_DOMAIN = (site_domain or "").lower()
            try:
                prof = get_links_from_x_profile(u, need_avatar=True)
                avatar_verified = (prof or {}).get("avatar", "") or ""
            except Exception:
                avatar_verified = ""
            return twitter_final, {}, "", avatar_verified

    for u in deduped:
        ok, extra, agg = verify_twitter_and_enrich(u, site_domain)
        if ok:
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
            return twitter_final, enriched_from_agg, aggregator_url, avatar_verified

    brand_like = [u for u in deduped if bt and bt in _handle(u)]
    twitter_final = brand_like[0] if brand_like else (deduped[0] if deduped else "")
    return twitter_final, {}, "", ""


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
