from __future__ import annotations

import json
import os
import re
import subprocess
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_setup import get_logger
from core.normalize import clean_project_name, force_https, is_bad_name

logger = get_logger("web")

# Кэши для экономии сетевых запросов
_FETCHED_HTML_CACHE: dict[str, str] = {}
_DOCS_LOGGED: set[str] = set()


# Нормализованный хост из URL
def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# Очень грубая эвристика подозрительного HTML (CF, редиректы и т.п.)
def is_html_suspicious(html: str) -> bool:
    if not html:
        return True
    low = html.lower()
    if any(
        s in low
        for s in (
            "cloudflare",
            "cf-challenge",
            "verifying you are human",
            "checking your browser",
            "just a moment",
        )
    ):
        return True
    return len(html) < 2500


# Быстрая проверка: есть ли ссылки на основные соцсети
def has_social_links(html: str) -> bool:
    low = html.lower()
    for dom in (
        "twitter.com",
        "x.com",
        "discord.gg",
        "t.me",
        "telegram.me",
        "github.com",
        "medium.com",
    ):
        if dom in low:
            return True
    return False


# Вызов Node-скрипта (Playwright) для получения HTML
def _browser_fetch(path_js: str, url: str, raw: bool = False, timeout: int = 60) -> str:
    try:
        args = ["node", path_js, url]
        if raw:
            args.append("--raw")
        res = subprocess.run(
            args,
            cwd=os.path.dirname(path_js),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if res.returncode == 0 and res.stdout:
            return res.stdout
        return res.stdout or res.stderr or ""
    except Exception as e:
        logger.warning("browser_fetch failed for %s: %s", url, e)
        return ""


# Получить HTML через Playwright (если нужен JS)
def fetch_url_html_playwright(url: str, timeout: int = 60) -> str:
    script_path = os.path.join(os.path.dirname(__file__), "browser_fetch.js")
    out = _browser_fetch(script_path, url, raw=False, timeout=timeout)
    return out or ""


# Главный загрузчик HTML: requests → (при необходимости) Playwright
def fetch_url_html(url: str, *, prefer: str = "auto", timeout: int = 30) -> str:
    url = force_https(url)
    if url in _FETCHED_HTML_CACHE:
        return _FETCHED_HTML_CACHE[url]

    if prefer == "http":
        try:
            r = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0"},
                allow_redirects=True,
            )
            html = r.text or ""
        except Exception as e:
            logger.warning("requests error %s: %s", url, e)
            html = ""
        _FETCHED_HTML_CACHE[url] = html
        return html

    if prefer == "browser":
        out = fetch_url_html_playwright(url)
        _FETCHED_HTML_CACHE[url] = out
        return out

    html = ""
    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
            allow_redirects=True,
        )
        html = r.text or ""
    except Exception as e:
        logger.warning("requests error %s: %s", url, e)

    if (not html) or is_html_suspicious(html):
        out = fetch_url_html_playwright(url)
        _FETCHED_HTML_CACHE[url] = out or html
        return _FETCHED_HTML_CACHE[url]

    _FETCHED_HTML_CACHE[url] = html
    return html


# Регулярки для распознавания соцсетей
_SOCIAL_PATTERNS = {
    "twitterURL": re.compile(r"(?:twitter\.com|x\.com)", re.I),
    "discordURL": re.compile(r"(?:discord\.gg|discord\.com)", re.I),
    "telegramURL": re.compile(r"(?:t\.me|telegram\.me)", re.I),
    "youtubeURL": re.compile(r"(?:youtube\.com|youtu\.be)", re.I),
    "linkedinURL": re.compile(r"(?:linkedin\.com)", re.I),
    "redditURL": re.compile(r"(?:reddit\.com)", re.I),
    "mediumURL": re.compile(r"(?:medium\.com)", re.I),
    "githubURL": re.compile(r"(?:github\.com)", re.I),
}


# Поиск «лучшей» ссылки на документацию на странице
def find_best_docs_link(soup: BeautifulSoup, base_url: str) -> str:
    cands: list[str] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        text = (a.text or "").strip().lower()
        if any(
            k in text for k in ("docs", "documentation", "developer docs", "developers")
        ):
            cands.append(href)

    filtered = [
        h
        for h in cands
        if not re.search(r"(api-docs|apidocs|developer-docs|/api($|/)|api\.)", h, re.I)
    ]

    def _score(h: str) -> int:
        p = urlparse(h)
        if re.match(r".*/docs/?$", p.path) and not p.netloc.startswith("api."):
            return 0
        if p.netloc.startswith("docs."):
            return 1
        return 2

    if filtered:
        filtered.sort(key=_score)
        doc = filtered[0]
        if doc not in _DOCS_LOGGED:
            logger.info("docs link found: %s", doc)
            _DOCS_LOGGED.add(doc)
        return doc

    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        p = urlparse(href)
        if re.match(r".*/docs/?$", p.path) or p.netloc.startswith("docs."):
            if href not in _DOCS_LOGGED:
                logger.info("docs link found (fallback): %s", href)
                _DOCS_LOGGED.add(href)
            return href
    return ""


# Парс соцсетей и docs из HTML сайта
def extract_social_links(html: str, base_url: str, is_main_page: bool = False) -> dict:
    # поддержка json-ответа (если api возвращает готовую структуру)
    try:
        j = json.loads(html or "")
        if isinstance(j, dict) and "websiteURL" in j:
            for k, v in list(j.items()):
                if isinstance(v, str):
                    j[k] = force_https(v)
            return j
    except Exception:
        pass

    soup = BeautifulSoup(html or "", "html.parser")
    links = {k: "" for k in _SOCIAL_PATTERNS}
    links["websiteURL"] = base_url
    links["documentURL"] = ""

    # cначала ищем в навигационных зонах
    zones = []
    zones.extend(soup.select("header, nav"))
    zones.extend(soup.select("footer"))
    zones.append(soup.find(["div", "section"], recursive=False))
    zones.append(soup.select_one("body > :last-child"))

    def _scan(node):
        if not node:
            return
        for a in node.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            for key, rx in _SOCIAL_PATTERNS.items():
                if not links[key] and rx.search(href):
                    links[key] = href

    for z in zones:
        _scan(z)

    # если ничего не нашли - полный перебор ссылок
    if all(not links[k] for k in links if k != "websiteURL"):
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            for key, rx in _SOCIAL_PATTERNS.items():
                if not links[key] and rx.search(href):
                    links[key] = href

    # документация
    doc = find_best_docs_link(soup, base_url)
    links["documentURL"] = doc or ""

    # фолбэк: если главная и ничего не нашли - пробуем браузерный fetch
    if is_main_page and all(
        not links[k] for k in links if k not in ("websiteURL", "documentURL")
    ):
        out = fetch_url_html_playwright(base_url)
        try:
            j = json.loads(out or "")
            if isinstance(j, dict) and "websiteURL" in j:
                for k, v in list(j.items()):
                    if isinstance(v, str):
                        j[k] = force_https(v)
                if doc and not j.get("documentURL"):
                    j["documentURL"] = doc
                return j
        except Exception:
            pass

    # нормализация схемы
    for k, v in list(links.items()):
        if isinstance(v, str) and v:
            links[k] = force_https(v)
    return links


# Попытка определить имя проекта из HTML/мета/титула/твиттера
def extract_project_name(
    html: str, base_url: str, twitter_display_name: str = ""
) -> str:
    # доверяем display name твиттера (если выглядит нормально)
    tw = clean_project_name(twitter_display_name or "")
    if tw and not is_bad_name(tw):
        return tw

    # json-ответ (редкий кейс)
    try:
        j = json.loads(html or "{}")
        if isinstance(j, dict):
            for key in ("pageTitle", "title", "ogSiteName", "siteName"):
                val = clean_project_name(str(j.get(key, "")).strip())
                if val and not is_bad_name(val):
                    return val
    except Exception:
        pass

    soup = BeautifulSoup(html or "", "html.parser")

    # og:site_name
    meta_site = soup.select_one(
        "meta[property='og:site_name'][content], meta[name='og:site_name'][content]"
    )
    if meta_site and meta_site.get("content"):
        v = clean_project_name(meta_site.get("content", "").strip())
        if v and not is_bad_name(v):
            return v

    # title c разбором разделителей
    raw_title = soup.title.string.strip() if (soup.title and soup.title.string) else ""
    domain_token = ""
    try:
        domain_token = urlparse(base_url).netloc.replace("www.", "").split(".")[0]
    except Exception:
        domain_token = ""

    if raw_title:
        parts = re.split(r"[|\-–—:•·⋅]+", raw_title)
        cands = []
        for p in parts:
            val = clean_project_name(p or "")
            if not val or is_bad_name(val):
                continue
            score = 0
            if domain_token and domain_token.lower() in val.lower():
                score += 100
            if 2 <= len(val) <= 40:
                score += 10
            cands.append((score, val))
        if cands:
            cands.sort(key=lambda x: (-x[0], len(x[1])))
            best = cands[0][1]
            if best and not is_bad_name(best):
                return best

    # шапка сайта
    header = soup.select_one("header") or soup.select_one("nav")
    if header:
        img = header.select_one("img[alt]")
        if img and img.get("alt"):
            v = clean_project_name(img.get("alt", "").strip())
            if v and not is_bad_name(v):
                return v
        h1 = header.select_one("h1")
        if h1 and h1.get_text(strip=True):
            v = clean_project_name(h1.get_text(strip=True))
            if v and not is_bad_name(v):
                return v

    # фолбэк - домен
    try:
        token = urlparse(base_url).netloc.replace("www.", "").split(".")[0]
        v = clean_project_name((token or "").capitalize())
        return v or "Project"
    except Exception:
        return "Project"


# Домен без www из URL
def get_domain_name(url: str) -> str:
    try:
        return urlparse(force_https(url)).netloc.replace("www.", "").lower()
    except Exception:
        return ""
