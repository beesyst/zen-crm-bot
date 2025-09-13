from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_setup import get_logger
from core.normalize import force_https, normalize_url, twitter_to_x
from core.settings import get_link_collections, get_social_hosts, get_social_keys

logger = get_logger("link_aggregator")


# Хелперы
def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _get_domains() -> list[str]:
    domains = get_link_collections()
    if not domains:
        raise RuntimeError(
            "config/settings.yml: 'link_collections' обязателен и не может быть пустым"
        )
    return domains


def is_link_aggregator(url: str | None) -> bool:
    if not url:
        return False
    host = _host(force_https(url))
    if not host:
        return False
    domains = _get_domains()
    return any(
        host == d or host == ("www." + d) or host.endswith("." + d) for d in domains
    )


def _is_social_host(h: str) -> bool:
    h = (h or "").lower()
    social_hosts = get_social_hosts()
    return any(h == s or h.endswith("." + s) for s in social_hosts)


def _normalize_socials_dict(d: dict) -> dict:
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, str) and v:
            vv = normalize_url(v)
            if k == "twitterURL":
                vv = twitter_to_x(vv)
            out[k] = vv
        else:
            out[k] = v
    return out


# Простой кэш HTML
_HTML_CACHE: dict[str, str] = {}


def _fetch_html(url: str, timeout: int = 20) -> str:
    u = force_https(url)
    if u in _HTML_CACHE:
        return _HTML_CACHE[u]
    try:
        resp = requests.get(u, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        html = resp.text or ""
    except Exception as e:
        logger.warning("Aggregator request failed: %s (%s)", u, e)
        html = ""
    _HTML_CACHE[u] = html
    return html


# Словарь соц-ссылок + websiteURL
def extract_socials_from_aggregator(agg_url: str) -> dict:
    # базовые ключи из settings.yml (socials.keys) + гарантируем наличие websiteURL
    keys = list(dict.fromkeys([*get_social_keys(), "websiteURL"]))
    out = {k: "" for k in keys}

    html = _fetch_html(agg_url)
    if not html:
        return out

    soup = BeautifulSoup(html, "html.parser")
    base_host = _host(agg_url)

    candidate_sites: list[str] = []

    def _emit(href: str):
        u = normalize_url(urljoin(agg_url, href))
        if not u:
            return
        h = _host(u)
        if not h:
            return

        # социки по доменам
        if (
            h in ("x.com", "twitter.com")
            or h.endswith(".x.com")
            or h.endswith(".twitter.com")
        ):
            if "twitterURL" in out and not out["twitterURL"]:
                out["twitterURL"] = twitter_to_x(u)
            return
        if h in ("t.me", "telegram.me"):
            if "telegramURL" in out and not out["telegramURL"]:
                out["telegramURL"] = u
            return
        if (
            h in ("discord.gg", "discord.com")
            or h.endswith(".discord.gg")
            or h.endswith(".discord.com")
        ):
            if "discordURL" in out and not out["discordURL"]:
                out["discordURL"] = u
            return
        if h in ("youtube.com", "youtu.be") or h.endswith(".youtube.com"):
            if "youtubeURL" in out and not out["youtubeURL"]:
                out["youtubeURL"] = u
            return
        if h in ("linkedin.com", "lnkd.in") or h.endswith(".linkedin.com"):
            if "linkedinURL" in out and not out["linkedinURL"]:
                out["linkedinURL"] = u
            return
        if h == "reddit.com" or h.endswith(".reddit.com"):
            if "redditURL" in out and not out["redditURL"]:
                out["redditURL"] = u
            return
        if h == "medium.com" or h.endswith(".medium.com"):
            if "mediumURL" in out and not out["mediumURL"]:
                out["mediumURL"] = u
            return
        if h == "github.com" or h.endswith(".github.com"):
            if "githubURL" in out and not out["githubURL"]:
                out["githubURL"] = u
            return

        # кандидаты на официальный сайт (не сам агрегатор и не соцхосты)
        if re.match(r"^https?://", u) and (not _is_social_host(h)) and (h != base_host):
            candidate_sites.append(u)

    # ссылки из <a>
    logger.debug("Aggregator parse start: %s", agg_url)
    for a in soup.find_all("a", href=True):
        _emit(urljoin(agg_url, a["href"]))
    logger.debug("Aggregator parsed: %s", {k: v for k, v in out.items() if v})

    # если явных кандидатов нет - попробуем canonical/og:url
    if not candidate_sites:
        canon = soup.select_one("link[rel=canonical][href]")
        if canon:
            _emit(canon["href"])
        og = soup.select_one("meta[property='og:url'][content]")
        if og:
            _emit(og["content"])

    # выберем лучший websiteURL
    if candidate_sites:
        seen = set()
        for u in candidate_sites:
            uu = normalize_url(u)
            if uu and uu not in seen:
                out["websiteURL"] = uu
                break

    # финальная нормализация
    return _normalize_socials_dict(out)


# Find + verify
def find_aggregators_in_links(links: list[str]) -> list[str]:
    res, seen = [], set()
    for u in links or []:
        if not u:
            continue
        if is_link_aggregator(u):
            uu = force_https(u).rstrip("/")
            if uu not in seen:
                res.append(uu)
                seen.add(uu)
    return res


# Подтверждение, что агрегатор относится к проекту
def verify_aggregator_belongs(
    agg_url: str, site_domain: str, handle: str | None
) -> tuple[bool, dict]:
    site_domain = (site_domain or "").lower().lstrip(".")
    html = _fetch_html(agg_url)
    if not html:
        return False, {}

    soup = BeautifulSoup(html, "html.parser")

    has_domain = False
    if site_domain:
        for a in soup.find_all("a", href=True):
            try:
                u = normalize_url(urljoin(agg_url, a["href"]))
                if _host(u).endswith(site_domain):
                    has_domain = True
                    break
            except Exception:
                continue

    has_handle = False
    if handle:
        # ищем именно ссылки на профиль/твиты этого хэндла
        rx = re.compile(
            r"(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com)/"
            + re.escape(handle)
            + r"(?:/|$)",
            re.I,
        )
        for a in soup.find_all("a", href=True):
            href = normalize_url(urljoin(agg_url, a["href"]))
            if rx.search(href):
                has_handle = True
                break
        if not has_handle:
            # мягкий фолбэк: по тексту (редкие био-агрегаторы)
            if rx.search(html):
                has_handle = True

    ok = bool(has_domain or has_handle)
    if ok:
        bits = extract_socials_from_aggregator(agg_url)
        logger.info(
            "Агрегатор %s подтвержден и спарсен: %s",
            agg_url,
            {k: v for k, v in bits.items() if v},
        )
    else:
        logger.info(
            "Агрегатор %s не подтвердился (handle=%s, domain=%s)",
            agg_url,
            handle,
            site_domain,
        )
        bits = {}

    return ok, bits
