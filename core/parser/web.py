from __future__ import annotations

import json
import os
import re
import subprocess
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_setup import get_logger
from core.normalize import clean_project_name, force_https, is_bad_name
from core.settings import (
    get_http_ua,
    get_settings,
    get_social_host_map,
    get_social_keys,
)

logger = get_logger("web")

# Глобальные кэши и константы
_FETCHED_HTML_CACHE: dict[str, str] = {}
_DOCS_LOGGED: set[str] = set()

UA = get_http_ua()


# Конфигурация (динамически из settings.yml)
_CFG = get_settings() or {}

# Короткие ключи соцсетей (website, document, twitter, ...)
_SOCIAL_KEYS: tuple[str, ...] = tuple(get_social_keys() or ())

# Host→key мапа (например: x.com→twitter, discord.gg→discord, ...)
_HOST_MAP: dict[str, str] = get_social_host_map() or {}

# Нормализованный список известных соц-доменов из host_map
_SOCIAL_HOSTS: tuple[str, ...] = tuple(_HOST_MAP.keys())


# Хелпер: нормализованный хост из URL (без www)
def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# Хелпер: абсолютный https-URL (поддержка относительных ссылок)
def _abs_https(base: str, u: str) -> str:
    try:
        if not isinstance(u, str) or not u:
            return ""
        vv = u if u.startswith("http") else urljoin(base, u)
        return force_https(vv)
    except Exception:
        return u or ""


# Хелпер: это соц-домен (по host_map)?
def _is_social_host(h: str, known: tuple[str, ...] = _SOCIAL_HOSTS) -> bool:
    h = (h or "").lower()
    if h in known:
        return True
    return any(h.endswith("." + base) for base in known)


# Хелперы HTTP: GET текст и HEAD/GET для финального URL
def _http_get_text(url: str, *, timeout: int) -> str:
    try:
        r = requests.get(
            force_https(url),
            timeout=timeout,
            headers={"User-Agent": UA},
            allow_redirects=True,
        )
        return r.text or ""
    except Exception as e:
        logger.warning("requests error %s: %s", url, e)
        return ""


def _http_head_or_get_final_url(url: str, *, timeout: int) -> str:
    u = force_https(url)
    try:
        r = requests.head(
            u, allow_redirects=True, timeout=timeout, headers={"User-Agent": UA}
        )
        return r.url or u
    except Exception:
        try:
            r = requests.get(
                u, allow_redirects=True, timeout=timeout, headers={"User-Agent": UA}
            )
            return r.url or u
        except Exception:
            return u


# Построение паттернов распознавания соцсетей по host_map
def _build_social_patterns() -> dict[str, re.Pattern]:
    buckets: dict[str, list[str]] = {}
    for host, key in _HOST_MAP.items():
        buckets.setdefault(key, []).append(re.escape(host))
    patterns: dict[str, re.Pattern] = {}
    for key, hosts in buckets.items():
        patt = r"(?:%s)" % "|".join([h.replace(r"\.", r"\.") for h in hosts])
        patterns[key] = re.compile(patt, re.I)
    return patterns


# Регулярки для распознавания соцсетей (динамически)
_SOCIAL_PATTERNS = _build_social_patterns()


# Есть ли в HTML ссылки на соцсети (по host_map)
def _html_has_any_social_host(html: str) -> bool:
    if not html:
        return False
    soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.find_all("a", href=True):
        h = _host(urljoin("https://example.org/", a["href"]))
        if h in _SOCIAL_HOSTS:
            return True
        for base in _SOCIAL_HOSTS:
            if h.endswith("." + base):
                return True
    return False


# Если короткая/редиректная ссылка: получить канонический https://x.com/<handle>
def _resolve_x_profile_via_redirect(u: str, timeout: int = 8) -> str:
    uu = force_https(u or "")
    if not uu:
        return ""
    prof = _extract_x_profile(uu)
    if prof:
        return prof

    try:
        p = urlparse(uu)
        q = parse_qs(p.query or "")

        screen = (q.get("screen_name") or [""])[0].strip()
        if screen and re.match(r"^[A-Za-z0-9_]{1,15}$", screen):
            return f"https://x.com/{screen}"

        redir = (q.get("redirect_after_login") or [""])[0]
        if redir:
            redir = force_https(unquote(redir))
            prof2 = _extract_x_profile(redir)
            if prof2:
                return prof2

        for key in ("url", "u", "to", "target", "redirect", "redirect_uri"):
            for cand in q.get(key, []):
                cand = force_https(unquote(cand or ""))
                prof2 = _extract_x_profile(cand)
                if prof2:
                    return prof2
    except Exception:
        pass

    final = _http_head_or_get_final_url(uu, timeout=timeout)
    return _extract_x_profile(final)


# Извлечь профиль X/Twitter из произвольного текста (резервная маска)
def _extract_x_profile_from_text(html: str) -> str:
    try:
        m = re.search(
            r"https?://(?:www\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,15})(?:[/?#]|\b)",
            html or "",
            flags=re.I,
        )
        return f"https://x.com/{m.group(1)}" if m else ""
    except Exception:
        return ""


# Строго нормализовать X/Twitter к виду https://x.com/<handle>
def _extract_x_profile(u: str | None) -> str:
    s = force_https(u or "")
    if not s:
        return ""
    try:
        p = urlparse(s)
        host = (p.netloc or "").split(":")[0].lower().replace("www.", "")
        is_twitter = host == "twitter.com" or host.endswith(".twitter.com")
        is_x = host == "x.com"
        if not (is_twitter or is_x):
            return ""

        seg = (p.path or "/").strip("/").split("/", 1)[0]
        if seg and re.match(r"^[A-Za-z0-9_]{1,15}$", seg):
            return f"https://x.com/{seg}"

        q = parse_qs(p.query or "")
        screen = (q.get("screen_name") or [""])[0].strip()
        if screen and re.match(r"^[A-Za-z0-9_]{1,15}$", screen):
            return f"https://x.com/{screen}"

        redir = (q.get("redirect_after_login") or [""])[0]
        if redir:
            redir = force_https(unquote(redir))
            try:
                rp = urlparse(redir)
                rseg = (rp.path or "/").strip("/").split("/", 1)[0]
                if rseg and re.match(r"^[A-Za-z0-9_]{1,15}$", rseg):
                    return f"https://x.com/{rseg}"
            except Exception:
                pass
        return ""
    except Exception:
        return ""


# Эвристика подозрительного HTML (CF/антибот/слишком малый DOM)
def is_html_suspicious(html: str) -> bool:
    if not html:
        return True

    try:
        j = json.loads(html)
        if isinstance(j, dict) and ("html" in j or "text" in j):
            return False
    except Exception:
        pass

    low = html.lower()

    if any(
        s in low
        for s in (
            "cf-browser-verification",
            "cloudflare",
            "cf-challenge",
            "verifying you are human",
            "checking your browser",
            "just a moment",
        )
    ):
        return True

    spa_marker = any(
        s in low
        for s in ('id="__next"', "data-reactroot", "ng-version", "vite", "data-radix-")
    )
    if spa_marker and len(html) < 2500 and not _html_has_any_social_host(html):
        return True

    if len(html) < 2000 and not _html_has_any_social_host(html):
        return True

    return False


# Быстрая проверка: есть ли соцссылки (по host_map)
def has_social_links(html: str) -> bool:
    return _html_has_any_social_host(html)


# Запуск Node-скрипта (Playwright) для HTML/соц-json
def _browser_fetch(path_js, url, timeout=60, wait="networkidle", mode="html") -> dict:
    try:
        args = [
            "node",
            path_js,
            "--url",
            url,
            "--wait",
            wait,
            "--timeout",
            str(int(timeout * 1000)),
            "--retries",
            "2",
            "--ua",
            UA,
        ]
        if mode == "html":
            args.append("--html")
        elif mode == "socials":
            args.append("--socials")

        res = subprocess.run(
            args,
            cwd=os.path.dirname(path_js),
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
        raw = res.stdout or res.stderr or ""
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": False, "html": "", "text": "", "error": raw}
    except Exception as e:
        logger.warning("browser_fetch failed for %s: %s", url, e)
        return {"ok": False, "html": "", "text": "", "error": str(e)}


# Получить HTML через Playwright (если нужен JS)
def fetch_url_html_playwright(url, timeout=60, wait="networkidle", mode="html") -> str:
    script_path = os.path.join(os.path.dirname(__file__), "browser_fetch.js")
    res = _browser_fetch(script_path, url, timeout=timeout, wait=wait, mode=mode)
    try:
        return json.dumps(res, ensure_ascii=False)
    except Exception:
        return ""


# Главный загрузчик HTML: requests → (при необходимости) Playwright
def fetch_url_html(url: str, *, prefer: str = "auto", timeout: int = 30) -> str:
    url = force_https(url)
    if url in _FETCHED_HTML_CACHE:
        return _FETCHED_HTML_CACHE[url]

    try:
        host = urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        host = ""
    if host in ("x.com", "twitter.com"):
        prefer = "browser"

    if prefer == "http":
        html = _http_get_text(url, timeout=timeout)
        _FETCHED_HTML_CACHE[url] = html
        return html

    if prefer == "browser":
        out = fetch_url_html_playwright(url, mode="html")
        _FETCHED_HTML_CACHE[url] = out
        return out

    html = _http_get_text(url, timeout=timeout)

    need_browser = (
        (not html) or is_html_suspicious(html) or (not _html_has_any_social_host(html))
    )
    if need_browser:
        out = fetch_url_html_playwright(
            url, timeout=max(80, timeout), wait="networkidle", mode="html"
        )
        if (not out) or is_html_suspicious(out):
            out = fetch_url_html_playwright(
                url, timeout=max(80, timeout), wait="domcontentloaded", mode="html"
            )

        try:
            parsed = json.loads(out or "{}")
        except Exception:
            parsed = {}
        need_socials = True
        if isinstance(parsed, dict):
            dom = parsed.get("html") or parsed.get("text") or ""
            if dom and _html_has_any_social_host(dom):
                need_socials = False
        if need_socials:
            out = fetch_url_html_playwright(
                url, timeout=max(80, timeout), wait="networkidle", mode="socials"
            )

        _FETCHED_HTML_CACHE[url] = out or html
        return _FETCHED_HTML_CACHE[url]

    _FETCHED_HTML_CACHE[url] = html
    return html


# Поиск лучшей ссылки на документацию на странице
def find_best_docs_link(soup: BeautifulSoup, base_url: str) -> str:
    def _is_good_text(txt: str) -> bool:
        low = (txt or "").strip().lower()
        return any(
            k in low
            for k in (
                "docs",
                "documentation",
                "developer docs",
                "developers",
                "developer",
                "build",
                "build with",
            )
        )

    def _is_bad_docs_url(u: str) -> bool:
        return bool(
            re.search(r"(?:^|/)(?:api-docs|apidocs|developer-docs)(?:/|$)", u, re.I)
        ) or bool(re.search(r"/api(?:$|/)|(^|\.)api\.", u, re.I))

    def _score(href: str) -> int:
        p = urlparse(href)
        score = 10
        if re.match(r".*/docs/?$", p.path) and not p.netloc.startswith("api."):
            score = 0
        elif p.netloc.startswith("docs."):
            score = 1
        return score

    def _verify_docs(url: str) -> bool:
        def _ok_by_hints(html: str) -> bool:
            html = (html or "")[:200_000].lower()
            if "404" in html and ("not found" in html or "page not found" in html):
                return False
            doc_hints = (
                "docs",
                "documentation",
                "sidebar",
                "docusaurus",
                "mkdocs",
                "readthedocs",
                "vuepress",
                "vitepress",
                "table-of-contents",
                "toc__",
                "navitems",
                "md-content",
                "md-sidebar",
                "docsearch",
            )
            return sum(1 for k in doc_hints if k in html) >= 2

        try:
            resp = requests.get(
                url,
                headers={"User-Agent": UA},
                timeout=12,
                allow_redirects=True,
            )
            if resp.status_code == 200 and _ok_by_hints(resp.text or ""):
                return True
        except Exception:
            pass

        try:
            payload = fetch_url_html_playwright(
                url, timeout=60, wait="domcontentloaded", mode="html"
            )
            j = json.loads(payload or "") if payload else {}
            dom = ""
            if isinstance(j, dict):
                dom = j.get("html") or j.get("text") or ""
            return bool(dom and _ok_by_hints(dom))
        except Exception:
            return False

    explicit_candidates: list[str] = []
    for a in soup.find_all("a", href=True):
        text = (a.text or "").strip()
        if not _is_good_text(text):
            continue
        href = urljoin(base_url, a["href"])
        if not _is_bad_docs_url(href):
            explicit_candidates.append(href)

    explicit_candidates = list(dict.fromkeys(explicit_candidates))
    if explicit_candidates:
        explicit_candidates.sort(key=_score)
        for cand in explicit_candidates:
            if _verify_docs(cand):
                if cand not in _DOCS_LOGGED:
                    logger.debug("docs link found: %s", cand)
                    _DOCS_LOGGED.add(cand)
                return cand

    try:
        parsed = urlparse(base_url)
        host = parsed.netloc
        guesses = [
            f"https://docs.{host}",
            urljoin(base_url, "/docs/"),
        ]
        guesses = list(dict.fromkeys(guesses))
        for g in guesses:
            if _is_bad_docs_url(g):
                continue
            if _verify_docs(g):
                if g not in _DOCS_LOGGED:
                    logger.info("docs link found (guessed): %s", g)
                    _DOCS_LOGGED.add(g)
                return g
    except Exception:
        pass

    tail_candidates: list[str] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        p = urlparse(href)
        if re.match(r".*/docs/?$", p.path) or p.netloc.startswith("docs."):
            if not _is_bad_docs_url(href):
                tail_candidates.append(href)

    tail_candidates = list(dict.fromkeys(tail_candidates))
    for cand in tail_candidates:
        if _verify_docs(cand):
            if cand not in _DOCS_LOGGED:
                logger.info("docs link found (fallback): %s", cand)
                _DOCS_LOGGED.add(cand)
            return cand

    return ""


# Парс соцсетей и docs из HTML сайта (короткие ключи)
def extract_social_links(html: str, base_url: str, is_main_page: bool = False) -> dict:
    # попытка распарсить как JSON (браузерный ответ)
    try:
        j = json.loads(html or "")
        if (
            isinstance(j, dict)
            and ("html" in j or "text" in j)
            and not j.get("website")
        ):
            html = j.get("html") or j.get("text") or ""

        elif isinstance(j, dict) and j.get("website"):
            allowed = set(_SOCIAL_KEYS) | {"twitter_all"}
            j_clean: dict = {}
            for k, v in j.items():
                if k in allowed and isinstance(v, (str, list)):
                    j_clean[k] = v

            for k, v in list(j_clean.items()):
                if isinstance(v, str) and v:
                    vv = _abs_https(base_url, v)
                    if k == "twitter":
                        prof = _extract_x_profile(
                            vv
                        ) or _resolve_x_profile_via_redirect(vv)
                        vv = prof or ""
                    j_clean[k] = vv
                elif isinstance(v, list):
                    j_clean[k] = [
                        _abs_https(base_url, x) for x in v if isinstance(x, str) and x
                    ]

            if j_clean.get("twitter"):
                prof = _extract_x_profile(
                    j_clean["twitter"]
                ) or _resolve_x_profile_via_redirect(j_clean["twitter"])
                j_clean["twitter"] = prof or ""

            if (not j_clean.get("twitter")) and isinstance(
                j_clean.get("twitter_all"), list
            ):
                for u in j_clean["twitter_all"]:
                    if isinstance(u, str) and u:
                        prof = _extract_x_profile(u) or _resolve_x_profile_via_redirect(
                            u
                        )
                        if prof:
                            j_clean["twitter"] = prof
                            break

            _init_keys = set(_SOCIAL_KEYS) | {"website", "document"}
            links = {k: "" for k in _init_keys if k != "twitter_all"}
            links["website"] = base_url
            links["document"] = ""

            for k in list(links.keys()) + ["document"]:
                if k in j_clean and j_clean[k]:
                    links[k] = j_clean[k] if isinstance(j_clean[k], str) else j_clean[k]

            twitter_all: list[str] = []
            if isinstance(j_clean.get("twitter_all"), list):
                for u in j_clean["twitter_all"]:
                    if isinstance(u, str) and u:
                        u2 = force_https(u.replace("twitter.com", "x.com"))
                        if u2 not in twitter_all:
                            twitter_all.append(u2)

            if not links.get("twitter") and twitter_all:
                links["twitter"] = twitter_all[0]

            discord_hosts = tuple(h for h, k in _HOST_MAP.items() if k == "discord")
            if links.get("discord"):
                if not any(x in links["discord"].lower() for x in discord_hosts):
                    links["discord"] = _abs_https(base_url, links["discord"])
                    if not any(x in links["discord"].lower() for x in discord_hosts):
                        resolved = _http_head_or_get_final_url(
                            links["discord"], timeout=8
                        )
                        try:
                            h = _host(resolved)
                            if any(
                                h == d or h.endswith("." + d) for d in discord_hosts
                            ):
                                links["discord"] = force_https(resolved)
                        except Exception:
                            pass

            for k, v in list(links.items()):
                if isinstance(v, str) and v:
                    links[k] = _abs_https(base_url, v)

            if twitter_all:
                links["twitter_all"] = list(dict.fromkeys(twitter_all))

            return links
    except Exception:
        pass

    # обычный html → dom-парсинг зон
    soup = BeautifulSoup(html or "", "html.parser")
    _init_keys = set(_SOCIAL_KEYS) | {"website", "document"}
    links = {k: "" for k in _init_keys if k != "twitter_all"}
    links["website"] = base_url
    links["document"] = ""
    twitter_all: list[str] = []

    def _maybe_add_twitter(href_abs: str):
        try:
            prof = _extract_x_profile(href_abs)
            if prof and prof not in twitter_all:
                twitter_all.append(prof)
        except Exception:
            pass

    zones = []
    zones.extend(soup.select("footer, footer *"))
    zones.extend(soup.select("[role='contentinfo'], [role='contentinfo'] *"))
    zones.extend(soup.select("header, header *, nav, nav *"))
    zones.extend(
        soup.select(
            "[class*='social'], [class*='sns'], [class*='footer'], "
            "[class*='follow'], [data-testid*='footer']"
        )
    )
    zones.append(soup.select_one("body > :first-child"))
    zones.append(soup.select_one("body > :last-child"))

    def _scan(node):
        if not node:
            return
        for a in node.find_all("a", href=True):
            href = _abs_https(base_url, a["href"])
            text = (a.get_text(" ", strip=True) or "").lower()
            rel = " ".join(a.get("rel") or []).lower()
            aria = (a.get("aria-label") or "").lower()

            for key, rx in _SOCIAL_PATTERNS.items():
                if links.get(key):
                    continue

                if key == "twitter":
                    prof = _extract_x_profile(href) or _resolve_x_profile_via_redirect(
                        href
                    )
                    if (not prof) and (
                        ("twitter" in text)
                        or ("x(" in text)
                        or ("x / twitter" in text)
                        or ("x-twitter" in text)
                        or ("twitter" in aria)
                    ):
                        prof = _resolve_x_profile_via_redirect(href)
                    if prof:
                        links[key] = prof
                else:
                    matched = (
                        rx.search(href)
                        or rx.search(text)
                        or rx.search(rel)
                        or rx.search(aria)
                    )
                    if matched:
                        links[key] = href
                    elif key == "discord" and ("discord" in text or "discord" in aria):
                        links[key] = href

            prof_for_all = _extract_x_profile(href)
            if not prof_for_all:
                if (
                    ("twitter" in text)
                    or ("x(" in text)
                    or ("x / twitter" in text)
                    or ("x-twitter" in text)
                    or ("twitter" in aria)
                ):
                    prof_for_all = _resolve_x_profile_via_redirect(href)
            if prof_for_all:
                _maybe_add_twitter(prof_for_all)

    for z in zones:
        _scan(z)

    if all(not links[k] for k in links if k != "website"):
        for a in soup.find_all("a", href=True):
            href = _abs_https(base_url, a["href"])
            text = (a.get_text(" ", strip=True) or "").lower()
            aria = (a.get("aria-label") or "").lower()

            if not links["twitter"]:
                prof = _extract_x_profile(href)
                if not prof and (
                    ("twitter" in text) or ("x(" in text) or ("twitter" in aria)
                ):
                    prof = _resolve_x_profile_via_redirect(href)
                if prof:
                    links["twitter"] = prof

            for key, rx in _SOCIAL_PATTERNS.items():
                if key == "twitter":
                    continue
                if not links[key] and (
                    rx.search(href) or rx.search(text) or rx.search(aria)
                ):
                    links[key] = href
                elif (
                    key == "discord"
                    and not links[key]
                    and ("discord" in text or "discord" in aria)
                ):
                    links[key] = href

            prof_for_all = _extract_x_profile(href)
            if not prof_for_all and (
                ("twitter" in text) or ("x(" in text) or ("twitter" in aria)
            ):
                prof_for_all = _resolve_x_profile_via_redirect(href)
            if prof_for_all:
                _maybe_add_twitter(prof_for_all)

    if not links.get("twitter"):
        for a in soup.find_all("a", href=True):
            href = _abs_https(base_url, a["href"])
            text = (a.get_text(" ", strip=True) or "").lower()
            aria = (a.get("aria-label") or "").lower()
            prof = _extract_x_profile(href) or _resolve_x_profile_via_redirect(href)
            if (not prof) and (
                ("twitter" in text) or ("x(" in text) or ("twitter" in aria)
            ):
                prof = _resolve_x_profile_via_redirect(href)
            if prof:
                links["twitter"] = prof
                break

    if not links.get("twitter"):
        prof = _extract_x_profile_from_text(html)
        if prof:
            links["twitter"] = prof

    doc = find_best_docs_link(soup, base_url)
    links["document"] = doc or ""

    if is_main_page and (not any(links[k] for k in _SOCIAL_KEYS if k != "website")):
        logger.info("browser_fetch socials partial fill: %s", base_url)
        payload = fetch_url_html_playwright(
            base_url, timeout=60, wait="networkidle", mode="socials"
        )
        try:
            j2 = json.loads(payload or "") or {}
        except Exception:
            j2 = {}

        if isinstance(j2, dict) and j2.get("website"):
            allowed = set(_SOCIAL_KEYS) | {"twitter_all"}
            j2 = {k: v for k, v in j2.items() if k in allowed}

            for k, v in list(j2.items()):
                if isinstance(v, str) and v:
                    vv = _abs_https(base_url, v)
                    if k == "twitter":
                        vv = (
                            _extract_x_profile(vv)
                            or _resolve_x_profile_via_redirect(vv)
                            or ""
                        )
                    j2[k] = vv
                elif isinstance(v, list):
                    j2[k] = [
                        _abs_https(base_url, x) for x in v if isinstance(x, str) and x
                    ]

            for k in list(links.keys()):
                if k in j2 and j2[k] and not links.get(k):
                    if k == "twitter":
                        prof = _extract_x_profile(
                            j2[k]
                        ) or _resolve_x_profile_via_redirect(j2[k])
                        if prof:
                            links[k] = prof
                    else:
                        links[k] = j2[k]

            if isinstance(j2.get("twitter_all"), list) and j2["twitter_all"]:
                links["twitter_all"] = list(
                    dict.fromkeys(
                        [
                            force_https(u.replace("twitter.com", "x.com"))
                            for u in j2["twitter_all"]
                            if isinstance(u, str) and u
                        ]
                    )
                )

            for k, v in list(links.items()):
                if isinstance(v, str) and v:
                    links[k] = _abs_https(base_url, v)

            return links

        if isinstance(j2, dict) and ("html" in j2 or "text" in j2):
            html2 = j2.get("html") or j2.get("text") or ""
            if html2:
                soup2 = BeautifulSoup(html2, "html.parser")

                zones2 = []
                zones2.extend(soup2.select("header, header *, nav, nav *"))
                zones2.extend(soup2.select("footer, footer *"))
                zones2.extend(
                    soup2.select("[role='contentinfo'], [role='contentinfo'] *")
                )
                zones2.extend(
                    soup2.select(
                        "[class*='social'], [class*='sns'], [class*='footer'], "
                        "[class*='follow'], [data-testid*='footer']"
                    )
                )

                def _scan2(node):
                    if not node:
                        return
                    for a in node.find_all("a", href=True):
                        href = _abs_https(base_url, a["href"])
                        text = (a.get_text(" ", strip=True) or "").lower()
                        rel = " ".join(a.get("rel") or []).lower()
                        aria = (a.get("aria-label") or "").lower()

                        for key, rx in _SOCIAL_PATTERNS.items():
                            if not links.get(key) and (
                                rx.search(href)
                                or rx.search(text)
                                or rx.search(rel)
                                or rx.search(aria)
                            ):
                                if key == "twitter":
                                    prof = _extract_x_profile(href) or (
                                        _resolve_x_profile_via_redirect(href)
                                        if (
                                            ("twitter" in text)
                                            or ("x(" in text)
                                            or ("twitter" in aria)
                                        )
                                        else ""
                                    )
                                    if prof:
                                        links[key] = prof
                                else:
                                    links[key] = href

                        prof_for_all = _extract_x_profile(href)
                        if not prof_for_all and (
                            ("twitter" in text) or ("x(" in text) or ("twitter" in aria)
                        ):
                            prof_for_all = _resolve_x_profile_via_redirect(href)
                        if prof_for_all:
                            _maybe_add_twitter(prof_for_all)

                for z in zones2:
                    _scan2(z)

                if all(not links[k] for k in links if k not in ("website", "document")):
                    for a in soup2.find_all("a", href=True):
                        href = _abs_https(base_url, a["href"])
                        text = (a.get_text(" ", strip=True) or "").lower()
                        rel = " ".join(a.get("rel") or []).lower()
                        aria = (a.get("aria-label") or "").lower()

                        for key, rx in _SOCIAL_PATTERNS.items():
                            if key == "twitter":
                                continue
                            if not links.get(key) and (
                                rx.search(href)
                                or rx.search(text)
                                or rx.search(rel)
                                or rx.search(aria)
                            ):
                                links[key] = href
                            elif (
                                key == "discord"
                                and not links.get(key)
                                and ("discord" in text or "discord" in aria)
                            ):
                                links[key] = href

                        if _SOCIAL_PATTERNS.get("twitter") and _SOCIAL_PATTERNS[
                            "twitter"
                        ].search(href):
                            _maybe_add_twitter(href)

                if not links.get("twitter"):
                    for a in soup2.find_all("a", href=True):
                        href = _abs_https(base_url, a["href"])
                        text = (a.get_text(" ", strip=True) or "").lower()
                        aria = (a.get("aria-label") or "").lower()
                        prof = _extract_x_profile(
                            href
                        ) or _resolve_x_profile_via_redirect(href)
                        if (not prof) and (
                            ("twitter" in text) or ("x(" in text) or ("twitter" in aria)
                        ):
                            prof = _resolve_x_profile_via_redirect(href)
                        if prof:
                            links["twitter"] = prof
                            break

                doc2 = find_best_docs_link(soup2, base_url)
                if doc2 and not links.get("document"):
                    links["document"] = doc2

            if not links.get("twitter"):
                m = re.search(
                    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,15})(?:[/?#]|\b)",
                    html or "",
                    flags=re.I,
                )
                if m:
                    links["twitter"] = f"https://x.com/{m.group(1)}"
                else:
                    for a in soup.find_all("a", href=True):
                        text = (a.get_text(" ", strip=True) or "").lower()
                        if (
                            ("twitter" in text)
                            or ("x(" in text)
                            or ("x / twitter" in text)
                            or ("x-twitter" in text)
                        ):
                            prof = _extract_x_profile(
                                a["href"]
                            ) or _resolve_x_profile_via_redirect(a["href"])
                            if prof:
                                links["twitter"] = prof
                                break

            if links.get("twitter"):
                prof = _extract_x_profile(links["twitter"])
                if not prof:
                    logger.warning("web: drop invalid twitter: %s", links["twitter"])
                    links["twitter"] = ""
                else:
                    links["twitter"] = prof

    for k, v in list(links.items()):
        if isinstance(v, str) and v:
            vv = _abs_https(base_url, v)
            if k == "twitter":
                vv = _extract_x_profile(vv) or _resolve_x_profile_via_redirect(vv) or ""
            links[k] = vv

    if links.get("discord"):
        discord_hosts = tuple(h for h, key in _HOST_MAP.items() if key == "discord")
        if not any(x in links["discord"].lower() for x in discord_hosts):
            links["discord"] = _abs_https(base_url, links["discord"])
            if not any(x in links["discord"].lower() for x in discord_hosts):
                resolved = _http_head_or_get_final_url(links["discord"], timeout=8)
                try:
                    h = _host(resolved)
                    if any(h == d or h.endswith("." + d) for d in discord_hosts):
                        links["discord"] = force_https(resolved)
                except Exception:
                    pass

    if twitter_all:
        links["twitter_all"] = list(dict.fromkeys(twitter_all))

    logger.info(
        "Начальное обогащение %s: %s", base_url, {k: v for k, v in links.items() if v}
    )
    return links


# Попытка определить имя проекта из HTML/мета/титула/твиттера
def extract_project_name(
    html: str, base_url: str, twitter_display_name: str = ""
) -> str:
    tw = clean_project_name(twitter_display_name or "")
    if tw and not is_bad_name(tw):
        return tw

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

    meta_site = soup.select_one(
        "meta[property='og:site_name'][content], meta[name='og:site_name'][content]"
    )
    if meta_site and meta_site.get("content"):
        v = clean_project_name(meta_site.get("content", "").strip())
        if v and not is_bad_name(v):
            return v

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


# Сброс кэшей (опционально, для тестов/дебага)
def reset_caches() -> None:
    try:
        _FETCHED_HTML_CACHE.clear()
        _DOCS_LOGGED.clear()
    except Exception:
        pass
