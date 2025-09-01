from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_setup import get_logger
from core.normalize import force_https
from core.settings import get_settings

logger = get_logger("parser.link_aggregator")


# Вспом: получить host без www для сравнения доменов
def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _get_domains() -> list[str]:
    cfg = get_settings()
    domains = cfg.get("link_collections") or []
    if not domains:
        raise RuntimeError(
            "config/settings.yml: 'link_collections' обязателен и не может быть пустым"
        )
    # убираем www., пробелы и приводим к нижнему регистру
    norm = []
    for d in domains:
        if not isinstance(d, str):
            continue
        dd = d.strip().lower()
        if dd.startswith("www."):
            dd = dd[4:]
        if dd:
            norm.append(dd)
    if not norm:
        raise RuntimeError(
            "config/settings.yml: 'link_collections' содержит некорректные значения"
        )
    return norm


# Проверка: является ли URL линк-агрегатором
def is_link_aggregator(url: str | None) -> bool:
    if not url:
        return False
    host = _host(url)
    domains = _get_domains()
    return any(host == d or host.endswith("." + d) for d in domains)


# Спарсить ссылки соцсетей со страницы агрегатора
def extract_socials_from_aggregator(agg_url: str) -> dict:
    out = {
        "twitterURL": "",
        "discordURL": "",
        "telegramURL": "",
        "youtubeURL": "",
        "linkedinURL": "",
        "redditURL": "",
        "mediumURL": "",
        "githubURL": "",
        "websiteURL": "",
    }
    try:
        resp = requests.get(
            force_https(agg_url), timeout=20, headers={"User-Agent": "Mozilla/5.0"}
        )
        html = resp.text or ""
    except Exception as e:
        logger.warning("Aggregator request failed: %s", e)
        return out

    soup = BeautifulSoup(html, "html.parser")

    def _emit(href: str):
        u = force_https(urljoin(agg_url, href))
        h = _host(u)
        if "x.com" in h or "twitter.com" in h:
            out["twitterURL"] = out["twitterURL"] or u
        elif "discord.gg" in h or "discord.com" in h:
            out["discordURL"] = out["discordURL"] or u
        elif h in ("t.me", "telegram.me"):
            out["telegramURL"] = out["telegramURL"] or u
        elif "youtube.com" in h or "youtu.be" in h:
            out["youtubeURL"] = out["youtubeURL"] or u
        elif "linkedin.com" in h:
            out["linkedinURL"] = out["linkedinURL"] or u
        elif "reddit.com" in h:
            out["redditURL"] = out["redditURL"] or u
        elif "medium.com" in h:
            out["mediumURL"] = out["mediumURL"] or u
        elif "github.com" in h:
            out["githubURL"] = out["githubURL"] or u
        else:
            if not out["websiteURL"] and re.match(r"^https?://", u):
                out["websiteURL"] = u

    for a in soup.find_all("a", href=True):
        _emit(a["href"])

    return out


# Найти и вернуть уникальные ссылки агрегаторов из произвольного списка ссылок
def find_aggregators_in_links(links: list[str]) -> list[str]:
    res = []
    seen = set()
    for u in links or []:
        if is_link_aggregator(u):
            uu = force_https(u)
            if uu not in seen:
                res.append(uu)
                seen.add(uu)
    return res


# Проверка, что агрегатор относится к проекту (по домену сайта или X-хэндлу)
def verify_aggregator_belongs(
    agg_url: str, site_domain: str, handle: str | None
) -> tuple[bool, dict]:
    try:
        resp = requests.get(
            force_https(agg_url), timeout=20, headers={"User-Agent": "Mozilla/5.0"}
        )
        html = resp.text or ""
    except Exception:
        return False, {}

    has_domain = bool(site_domain and site_domain.lower() in (html.lower()))
    has_handle = False
    if handle:
        import re as _re

        rx = _re.compile(r"(?:x\.com|twitter\.com)/" + _re.escape(handle), _re.I)
        has_handle = bool(rx.search(html))

    ok = has_domain or has_handle
    bits = extract_socials_from_aggregator(agg_url) if ok else {}
    return ok, bits
