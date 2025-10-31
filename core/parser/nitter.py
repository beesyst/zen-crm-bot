from __future__ import annotations

import json
import os
import random
import re
import subprocess
import time
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from core.log_setup import get_logger
from core.normalize import force_https
from core.settings import get_http_ua, get_settings

logger = get_logger("nitter")
SETTINGS = get_settings() or {}
UA = get_http_ua()

# Загрузка конфига parser.nitter
_CFG = SETTINGS.get("parser", {}).get("nitter", {}) or {}

# Нормализация списка инстансов к https и без завершающего слеша
_INSTANCES: list[str] = []
for _u in _CFG.get("instances") or []:
    try:
        u = str(_u).strip()
        if not u:
            continue
        if u.startswith("http://"):
            u = "https://" + u[7:]
        elif not u.startswith("http"):
            u = "https://" + u.lstrip("/")
        u = u.rstrip("/")
        _INSTANCES.append(u)
    except Exception:
        continue

_ENABLED = bool(_CFG.get("enabled", True))
_TIMEOUT = int(_CFG.get("timeout") or 10)
_BAD_TTL = int(_CFG.get("bad_ttl") or 600)
_MAX_INS = int(_CFG.get("max_ins") or 3)
_STRATEGY = (_CFG.get("strategy") or "random").lower()

# Кэш HTML и бан-лист инстансов
_NITTER_HTML_CACHE: dict[str, tuple[str, str]] = {}
_NITTER_BAD: dict[str, float] = {}

# Состояние round-robin курсора (в памяти процесса)
_RR_COUNTER = {"idx": 0}


# Утилита: возврат списка живых nitter-инстансов с учетом TTL-бана
def _alive_instances() -> list[str]:
    t = time.time()
    alive = []
    for inst in _INSTANCES:
        s = force_https(inst).rstrip("/")
        if _NITTER_BAD.get(s, 0) <= t:
            alive.append(s)
    return alive


# Утилита: инстанс в бан на BAD_TTL секунд
def _ban(inst: str) -> None:
    _NITTER_BAD[force_https(inst).rstrip("/")] = time.time() + max(60, _BAD_TTL)


# Утилита: проверка, что HTML относится к нужному @handle
def _html_matches_handle(html: str, handle: str) -> bool:
    if not html or not handle:
        return False
    low = html.lower()
    h = handle.lower()
    if re.search(rf'href\s*=\s*["\']/\s*{re.escape(h)}(?:["\'/?# ]|$)', low):
        return True
    if re.search(rf"@{re.escape(h)}(?:[\"\' <]|$)", low):
        return True
    if "profile-card" in low and h in low:
        return True
    return False


# Утилита: эвристика антибота/пустышки: слишком короткий HTML или типичные фразы
def _looks_antibot(text: str) -> bool:
    low = (text or "").lower()
    if "tweet-body" in low or "timeline-item" in low:
        return False
    needles = (
        "captcha",
        "verify",
        "are you human",
        "access denied",
        "rate limit",
        "please enable javascript",
        "just a moment",
        "checking your browser",
    )
    return any(s in low for s in needles) or len(low) < 400


# Утилита: поход в URL через локальный playwright.js (без [web]-логов)
def _run_playwright(url: str, timeout_sec: int) -> tuple[str, int, str]:
    script = os.path.join(os.path.dirname(__file__), "playwright.js")
    args = [
        "node",
        script,
        "--url",
        url,
        "--wait",
        "networkidle",
        "--timeout",
        str(int(max(1, timeout_sec) * 1000)),
        "--retries",
        "1",
        "--ua",
        UA,
        "--raw",
        "--nitter",
        "true",
    ]
    try:
        res = subprocess.run(
            args,
            cwd=os.path.dirname(script),
            capture_output=True,
            text=True,
            timeout=max(timeout_sec + 10, 25),
        )
    except Exception as e:
        logger.debug("playwright run error for %s: %s", url, e)
        return "", 0, "runner_failed"

    raw = (res.stdout or "").strip()
    try:
        data = json.loads(raw) if raw.startswith("{") else {}
    except Exception:
        data = {}

    if not isinstance(data, dict):
        return "", 0, "bad_payload"

    html = (data.get("html") or data.get("text") or "") or ""
    status = int(data.get("status", 0) or 0)
    kind = (data.get("antiBot") or {}).get("kind", "")
    return html.strip(), status, kind


# Утилита: быстрая проба профиля: ава и ссылки из BIO/website
def _probe_profile(
    html: str, inst_base: str, handle: str
) -> tuple[str, str, list[str]]:
    if not html:
        return "", "", []
    soup = BeautifulSoup(html, "html.parser")

    base = f"{force_https(inst_base).rstrip('/')}/{handle}"
    links, seen = set(), set()
    for sel in (
        ".profile-card .profile-website a",
        ".profile-card .profile-bio a",
        ".profile-website a",
        ".profile-bio a",
        ".profile-card-extra a",
        'a[rel="me"]',
    ):
        for a in soup.select(sel):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            try:
                abs_u = urljoin(base, href)
            except Exception:
                abs_u = href
            if abs_u.startswith("//"):
                abs_u = "https:" + abs_u
            if not abs_u.startswith("http"):
                continue
            u = force_https(abs_u)
            if u not in seen:
                links.add(u)
                seen.add(u)

    avatar_raw, avatar_norm = _pick_avatar_from_soup(soup, inst_base)
    avatar_norm = _normalize_avatar(avatar_norm or "")

    return avatar_raw, avatar_norm, list(links)


# Утилита: возврат до max_count уникальных живых инстансов согласно стратегии
def _sample_instances_unique(max_count: int) -> list[str]:
    alive = _alive_instances()
    if not alive:
        return []

    if _STRATEGY == "round_robin":
        n = len(alive)
        out = []
        start = _RR_COUNTER["idx"] % n
        i = start
        while len(out) < min(max_count, n):
            out.append(alive[i % n])
            i += 1
        _RR_COUNTER["idx"] = (start + len(out)) % n
        return out

    pool = alive[:]
    random.shuffle(pool)
    return pool[: min(max_count, len(pool))]


# Утилита: декодер nitter /pic/<encoded> в прямой https-URL
def _decode_nitter_pic_url(src: str) -> str:
    s = (src or "").strip()
    if s.startswith("/pic/"):
        s = s[len("/pic/") :]
    s = unquote(s)
    if re.match(r"^/?(orig|media)/", s, re.I) and not s.startswith("http"):
        s = "https://pbs.twimg.com/" + s.lstrip("/")

    if s.startswith("//"):
        s = "https:" + s
    elif s.startswith("http://"):
        s = "https://" + s[7:]
    elif s.startswith("https://"):
        pass
    else:
        s = "https://" + s.lstrip("/")
    return s


# Утилита: привод URL авы к чистому https без query/fragment; декодер /pic/
def _normalize_avatar(url: str | None) -> str:
    u = force_https(url or "")
    if not u:
        return ""
    try:
        p = urlparse(u)
        if "/pic/" in (p.path or ""):
            return _decode_nitter_pic_url(p.path)
    except Exception:
        pass
    if u.startswith("/pic/"):
        u = _decode_nitter_pic_url(u)
    if u.startswith("pbs.twimg.com/"):
        u = "https://" + u
    u = re.sub(r"(?:\?[^#]*)?(?:#.*)?$", "", u)
    return u


# Утилита: поиск авы в разметке профиля (raw=/pic/..., normalized=https://pbs...)
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

    meta = soup.select_one(
        "meta[property='og:image'], meta[name='og:image'], meta[property='twitter:image:src']"
    )
    if meta:
        c = (meta.get("content") or meta.attrs.get("content") or "").strip()
        if c:
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
                c2 = force_https(c)
                return c2, c2
    return "", ""


# Получение HTML профиля через Nitter
def fetch_profile_html(handle: str, probe_log: bool = True) -> tuple[str, str]:
    if not handle:
        return "", ""

    if _ENABLED and _INSTANCES:
        candidates = _sample_instances_unique(max(1, _MAX_INS))
        for inst in candidates:
            base = force_https(inst).rstrip("/")
            cache_key = f"{base}|{handle.lower()}"
            if cache_key in _NITTER_HTML_CACHE:
                return _NITTER_HTML_CACHE[cache_key]

            url = f"{base}/{handle}"
            html, status, kind = _run_playwright(url, _TIMEOUT)

            # лог для режима 2
            if probe_log:
                avatar_raw, avatar_norm, links = _probe_profile(html, base, handle)
                try:
                    logger.info(
                        "Nitter GET+parse: %s/%s → avatar=%s, links=%d",
                        base,
                        handle,
                        "yes" if (avatar_raw or avatar_norm) else "no",
                        len(links),
                    )
                    if avatar_raw or avatar_norm:
                        logger.info(
                            "Avatar URL: %s", force_https(avatar_raw or avatar_norm)
                        )
                    if links:
                        logger.info("BIO из Nitter: %s", list(links))
                except Exception:
                    pass

            # валидация HTML профиля
            if html and _html_matches_handle(html, handle) and not _looks_antibot(html):
                _NITTER_HTML_CACHE[cache_key] = (html, base)
                return html, base

            # баним проблемные инстансы
            if (
                kind
                or status in (0, 403, 429, 503)
                or _looks_antibot(html)
                or (status == 200 and not _html_matches_handle(html, handle))
                or not html
            ):
                _ban(base)

    return "", ""


# Распарсить профиль (handle или x.com URL) и извлечь ссылки/аватар/имя
def parse_profile(url_or_handle: str) -> dict:
    if not url_or_handle:
        return {}

    handle = ""
    s = (url_or_handle or "").strip()
    m = re.match(r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/?$", (s + "/"), re.I)
    if m:
        handle = m.group(1)
    else:
        mm = re.match(r"^@?([A-Za-z0-9_]{1,15})$", s)
        handle = mm.group(1) if mm else ""

    if not handle:
        return {}

    html, inst = fetch_profile_html(handle)
    if not html or not inst:
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
        ".profile-card-extra a",
        'a[rel="me"]',
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
            if abs_u.startswith("//"):
                abs_u = "https:" + abs_u
            if not abs_u.startswith("http"):
                continue
            u = force_https(abs_u)
            if u not in seen:
                links.add(u)
                seen.add(u)

    if not links:
        blocks = []
        for m in re.finditer(
            r'<div\s+class="profile-bio"[^>]*>(.*?)</div>|'
            r'<div\s+class="profile-website"[^>]*>(.*?)</div>',
            html,
            flags=re.I | re.S,
        ):
            for grp in (1, 2):
                chunk = m.group(grp) or ""
                if chunk:
                    blocks.append(chunk)
        hrefs = []
        for chunk in blocks:
            for mm in re.finditer(r'href\s*=\s*["\']([^"\']+)["\']', chunk, flags=re.I):
                hrefs.append(mm.group(1).strip())
        for href in hrefs:
            if not href:
                continue
            try:
                abs_u = urljoin(base, href)
            except Exception:
                abs_u = href
            if abs_u.startswith("//"):
                abs_u = "https:" + abs_u
            if not abs_u.startswith("http"):
                continue
            u = force_https(abs_u)
            if u not in seen:
                links.add(u)
                seen.add(u)

    avatar_raw, avatar_norm = _pick_avatar_from_soup(soup, inst)
    avatar_norm = _normalize_avatar(avatar_norm or "")

    return {
        "links": list(links),
        "avatar": avatar_norm,
        "avatar_raw": force_https(avatar_raw or ""),
        "name": name,
    }


# Извлечение твитов из HTML профиля Nitter
def _extract_tweet_items(soup, inst_base, handle, limit: int) -> list[dict]:
    # локальные декодеры, чтобы не плодить повторные походы в twitter.py
    def _decode_video(u: str) -> str:
        try:
            p = urlparse(u)
            if "/video/" in (p.path or ""):
                encoded = u.split("/video/", 1)[1].split("/", 1)[1]
                decoded = unquote(encoded).replace("&amp;", "&")
                return force_https(decoded).rstrip("/")
        except Exception:
            pass
        return force_https(u).rstrip("/")

    # /pic/<encoded> → https://pbs.twimg.com/...
    def _decode_pic(u: str) -> str:
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

    def _dedup(seq: list[str]) -> list[str]:
        seen, out = set(), []
        for x in seq or []:
            xx = force_https(x).rstrip("/")
            if xx and xx not in seen:
                seen.add(xx)
                out.append(xx)
        return out

    items = []
    base = force_https(inst_base).rstrip("/")

    # поддерживаем обе разметки: article.timeline-item ИЛИ div.tweet-body
    for art in soup.select("article.timeline-item, div.tweet-body")[: max(1, limit)]:
        try:
            # id/URL
            a_date = art.select_one(".tweet-date a[href]")
            href = a_date.get("href").strip() if a_date else ""
            m = re.search(r"/status/(\d+)", href or "", re.I)
            tw_id = m.group(1) if m else ""
            status_url = f"https://x.com/{handle}/status/{tw_id}" if tw_id else ""

            # дата
            t = art.select_one(".tweet-date time[datetime]")
            dt = (t.get("datetime") or "").strip() if t else ""

            # текст
            text_el = art.select_one(".tweet-content, .tweet-content.media-body")
            text = text_el.get_text(" ", strip=True) if text_el else ""
            title = (text[:117] + "…") if len(text) > 120 else text

            # изображения (как было)
            images = []
            for img in art.select(
                "a.attachments img[src], div.attachment img[src], "
                "div.attachments img[src], div.gallery-row img[src], "
                "div.attachment.image img[src], a.still-image img[src]"
            ):
                src = (img.get("src") or "").strip()
                if src:
                    images.append(urljoin(base, src))
            images = _dedup([_decode_pic(u) for u in images])

            # видео: вытаскиваем прямо отсюда (без второго захода на статус)
            videos_raw, posters_raw = [], []

            # <video data-url|src poster=...>
            for vc in art.select(
                "div.gallery-video div.attachment.video-container, div.attachment.video-container"
            ):
                v = vc.select_one("video[data-url], video[src]")
                if v:
                    mu = (v.get("data-url") or v.get("src") or "").strip()
                    if mu:
                        videos_raw.append(urljoin(base, mu))
                    poster = (v.get("poster") or "").strip()
                    if poster:
                        posters_raw.append(urljoin(base, poster))
                for ssrc in vc.select("source[src]"):
                    su = (ssrc.get("src") or "").strip()
                    if su:
                        videos_raw.append(urljoin(base, su))

            # ссылки вида <a href="/video/...">
            for a in art.select("a[href*='/video/']"):
                hu = (a.get("href") or "").strip()
                if hu:
                    videos_raw.append(urljoin(base, hu))

            # постеры как still-image
            for a in art.select("a.still-image[href], a[href^='/pic/']"):
                hu = (a.get("href") or "").strip()
                if hu:
                    posters_raw.append(urljoin(base, hu))

            videos = _dedup([_decode_video(u) for u in videos_raw])
            posters = _dedup([_decode_pic(u) for u in posters_raw])

            # attachments: первым - m3u8, вторым - постер (если есть)
            attachments: list[str] = []
            if videos:
                attachments.append(videos[0])
            if posters:
                attachments.append(posters[0])

            items.append(
                {
                    "id": tw_id,
                    "status_url": status_url,
                    "handle": handle,
                    "datetime": dt,
                    "text": text,
                    "title": title,
                    "media": images,
                    "images": images,
                    "videos": videos,
                    "attachments": attachments,
                    "nitter_base": inst_base,
                }
            )
        except Exception:
            continue
    return items


def parse_tweets_from_html(
    html: str, inst_base: str, handle: str, limit: int = 5
) -> list[dict]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    return _extract_tweet_items(soup, inst_base, handle, limit)


# Загрузка HTML профиля через Nitter/Playwright и парсинг последних твитов
def fetch_tweets(
    handle: str, limit: int = 5, oldest_days: int | None = None
) -> list[dict]:
    html, inst = fetch_profile_html(handle, probe_log=False)
    items: list[dict] = []

    if html and inst:
        # берем avatar/links из того же HTML, чтобы собрать единый лог
        avatar_raw, avatar_norm, links = _probe_profile(html, inst, handle)
        soup = BeautifulSoup(html, "html.parser")
        items = _extract_tweet_items(soup, inst, handle, limit=limit)

        try:
            logger.info(
                "Nitter GET+parse: %s/%s → avatar=%s, links=%d, tweets=%d",
                (inst or "").rstrip("/"),
                handle,
                "yes" if (avatar_raw or avatar_norm) else "no",
                len(links or []),
                len(items or []),
            )
            if avatar_raw or avatar_norm:
                logger.info("Avatar URL: %s", force_https(avatar_raw or avatar_norm))
            if links:
                logger.info("BIO из Nitter: %s", list(links))
        except Exception:
            pass

    return items or []
