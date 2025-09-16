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
from core.settings import get_settings

logger = get_logger("web")

# Кэши для экономии сетевых запросов
_FETCHED_HTML_CACHE: dict[str, str] = {}
_DOCS_LOGGED: set[str] = set()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_CFG = get_settings() or {}
_socials = _CFG.get("socials") or {}
_SOCIAL_KEYS: tuple[str, ...] = tuple(_socials.get("keys") or ())
_SOCIAL_HOSTS: tuple[str, ...] = tuple(_socials.get("social_hosts") or ())


# Нормализованный хост из URL
def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# Если ссылка короткая (bit.ly/lnkd.in/…): редирект и возврат каноническего https://x.com/<handle>
def _resolve_x_profile_via_redirect(u: str, timeout: int = 8) -> str:
    uu = force_https(u or "")
    if not uu:
        return ""
    # быстрый кейс
    prof = _extract_x_profile(uu)
    if prof:
        return prof

    from urllib.parse import parse_qs, unquote, urlparse

    try:
        p = urlparse(uu)
        q = parse_qs(p.query or "")

        # intent & login кейсы
        screen = (q.get("screen_name") or [""])[0].strip()
        if screen and re.match(r"^[A-Za-z0-9_]{1,15}$", screen):
            return f"https://x.com/{screen}"

        redir = (q.get("redirect_after_login") or [""])[0]
        if redir:
            redir = force_https(unquote(redir))
            prof2 = _extract_x_profile(redir)
            if prof2:
                return prof2

        # generic ?url/u/to/target=
        for key in ("url", "u", "to", "target", "redirect", "redirect_uri"):
            for cand in q.get(key, []):
                cand = force_https(unquote(cand or ""))
                prof2 = _extract_x_profile(cand)
                if prof2:
                    return prof2
    except Exception:
        pass

    # сеть
    try:
        r = requests.head(
            uu, allow_redirects=True, timeout=timeout, headers={"User-Agent": UA}
        )
        final = r.url or uu
    except Exception:
        try:
            r = requests.get(
                uu, allow_redirects=True, timeout=timeout, headers={"User-Agent": UA}
            )
            final = r.url or uu
        except Exception:
            final = uu

    return _extract_x_profile(final)


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


# Строго парс профиль X/Twitter вида https://x.com/<handle>
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

        # https://x.com/<handle>
        seg = (p.path or "/").strip("/").split("/", 1)[0]
        if seg and re.match(r"^[A-Za-z0-9_]{1,15}$", seg):
            return f"https://x.com/{seg}"

        q = parse_qs(p.query or "")
        # intent/follow?screen_name=Handle
        screen = (q.get("screen_name") or [""])[0].strip()
        if screen and re.match(r"^[A-Za-z0-9_]{1,15}$", screen):
            return f"https://x.com/{screen}"

        # i/flow/login?redirect_after_login=%2FXPLA_Official
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


# Грубая эвристика подозрительного HTML (CF, редиректы и т.п.)
def is_html_suspicious(html: str) -> bool:
    if not html:
        return True

    # если это json от браузера с полями ok/html/text - не считаем подозрительными
    try:
        j = json.loads(html)
        if isinstance(j, dict) and ("html" in j or "text" in j):
            return False
    except Exception:
        pass

    low = html.lower()

    # антибот/CF
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

    # spa-маркеры сами по себе - не повод паниковать, просто триггер попробовать браузер
    spa_marker = any(
        s in low
        for s in ('id="__next"', "data-reactroot", "ng-version", "vite", "data-radix-")
    )
    if spa_marker and len(html) < 2500 and not has_social_links(html):
        return True

    if len(html) < 2000 and not has_social_links(html):
        return True

    return False


# Быстрая проверка: есть ли ссылки на основные соцсети
def has_social_links(html: str) -> bool:
    soup = BeautifulSoup(html or "", "html.parser")
    host_set = set(_SOCIAL_HOSTS)

    # есть соцсети только если есть реальный якорь <a href=...> на один из social_hosts
    for a in soup.find_all("a", href=True):
        h = _host(urljoin("https://example.org/", a["href"]))
        if h in host_set:
            return True

    return False


# Вызов Node-скрипта (Playwright) для получения HTML
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

    # twitter всегда через браузер (редиректы, защита, spa)
    try:
        host = urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        host = ""
    if host in ("x.com", "twitter.com"):
        prefer = "browser"

    if prefer == "http":
        try:
            r = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": UA},
                allow_redirects=True,
            )
            html = r.text or ""
        except Exception as e:
            logger.warning("requests error %s: %s", url, e)
            html = ""
        _FETCHED_HTML_CACHE[url] = html
        return html

    if prefer == "browser":
        out = fetch_url_html_playwright(url, mode="html")
        _FETCHED_HTML_CACHE[url] = out
        return out

    html = ""
    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": UA},
            allow_redirects=True,
        )
        html = r.text or ""
    except Exception as e:
        logger.warning("requests error %s: %s", url, e)

    def _needs_browser_for_key_socials(html: str) -> bool:
        low = (html or "").lower()
        return not (
            "twitter.com" in low
            or "x.com" in low
            or "discord.gg" in low
            or "discord.com" in low
        )

    # если нет ключевых соцсетей (twitter/discord) — всё равно идём в Playwright
    if (not html) or is_html_suspicious(html) or _needs_browser_for_key_socials(html):
        out = fetch_url_html_playwright(
            url, timeout=max(80, timeout), wait="networkidle", mode="html"
        )
        if (not out) or is_html_suspicious(out):
            out = fetch_url_html_playwright(
                url, timeout=max(80, timeout), wait="domcontentloaded", mode="html"
            )

        # если в dom по-прежнему пусто - просим соц-JSON
        try:
            parsed = json.loads(out or "{}")
        except Exception:
            parsed = {}
        need_socials = True
        if isinstance(parsed, dict):
            dom = parsed.get("html") or parsed.get("text") or ""
            if dom and has_social_links(dom):
                need_socials = False
        if need_socials:
            out = fetch_url_html_playwright(
                url, timeout=max(80, timeout), wait="networkidle", mode="socials"
            )

        _FETCHED_HTML_CACHE[url] = out or html
        return _FETCHED_HTML_CACHE[url]

    _FETCHED_HTML_CACHE[url] = html
    return html


# Регулярки для распознавания соцсетей
_SOCIAL_PATTERNS = {
    "twitterURL": re.compile(r"\b(?:twitter\.com|x\.com)\b", re.I),
    "discordURL": re.compile(r"(?:discord\.gg|discord\.com)", re.I),
    "telegramURL": re.compile(r"(?:t\.me|telegram\.me)", re.I),
    "youtubeURL": re.compile(r"(?:youtube\.com|youtu\.be)", re.I),
    "linkedinURL": re.compile(r"(?:linkedin\.com(?:/company/|/in/)?|lnkd\.in)", re.I),
    "redditURL": re.compile(r"(?:reddit\.com)", re.I),
    "mediumURL": re.compile(r"(?:medium\.com)", re.I),
    "githubURL": re.compile(r"(?:github\.com)", re.I),
}


# Поиск «лучшей» ссылки на документацию на странице
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

    # отсеиваем API-списки и специфичные разделы, которые обычно не являются "главной" докой
    def _is_bad_docs_url(u: str) -> bool:
        return bool(
            re.search(r"(?:^|/)(?:api-docs|apidocs|developer-docs)(?:/|$)", u, re.I)
        ) or bool(re.search(r"/api(?:$|/)|(^|\.)api\.", u, re.I))

    # чем ниже - тем лучше
    def _score(href: str) -> int:
        p = urlparse(href)
        score = 10
        if re.match(r".*/docs/?$", p.path) and not p.netloc.startswith("api."):
            score = 0
        elif p.netloc.startswith("docs."):
            score = 1
        return score

    # проверяем, что url отдает 200 и выглядит как документация
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

        # requests
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

        # фолбэк: playwright (если Cloudflare/SPA/редиректы мешают)
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

    # собираем явные кандидаты со страницы
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

    # эвристические кандидаты: docs.<host> и <base>/docs
    try:
        parsed = urlparse(base_url)
        host = parsed.netloc
        guesses = [
            f"https://docs.{host}",
            urljoin(base_url, "/docs/"),
        ]
        # dedupe, сохранить порядок
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

    # последний шанс: любые ссылки типа .../docs или поддомен docs.*
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


# Парс соцсетей и docs из HTML сайта
def extract_social_links(html: str, base_url: str, is_main_page: bool = False) -> dict:
    from urllib.parse import urljoin

    # helpers
    def _abs(base: str, u: str) -> str:
        try:
            if not isinstance(u, str) or not u:
                return ""
            return u if u.startswith("http") else urljoin(base, u)
        except Exception:
            return u or ""

    def _resolve_external(u: str, want_hosts: tuple[str, ...], timeout: int = 8) -> str:
        if not u:
            return ""
        try:
            r = requests.head(
                u, allow_redirects=True, timeout=timeout, headers={"User-Agent": UA}
            )
            final = r.url or u
        except Exception:
            try:
                r = requests.get(
                    u, allow_redirects=True, timeout=timeout, headers={"User-Agent": UA}
                )
                final = r.url or u
            except Exception:
                final = u
        try:
            h = urlparse(final).netloc.lower().replace("www.", "")
            if any(h.endswith(d) for d in want_hosts):
                return force_https(final)
        except Exception:
            pass
        return ""

    # попытка распарсить как JSON
    try:
        j = json.loads(html or "")
        # пакет браузера: {ok, html|text, ...} - достаем dom и продолжаем обычный парсинг
        if (
            isinstance(j, dict)
            and ("html" in j or "text" in j)
            and not j.get("websiteURL")
        ):
            html = j.get("html") or j.get("text") or ""

        # соц-json (websiteURL присутствует) - аккуратно чистим/нормализуем и возвращаем
        elif isinstance(j, dict) and j.get("websiteURL"):
            allowed = set(_SOCIAL_KEYS) | {"twitterAll"}
            j_clean: dict = {}
            for k, v in j.items():
                if k in allowed and isinstance(v, (str, list)):
                    j_clean[k] = v

            # абсолютные + https; twitter.com → x.com; списки тоже нормализуем
            for k, v in list(j_clean.items()):
                if isinstance(v, str) and v:
                    vv = force_https(_abs(base_url, v))
                    if k == "twitterURL":
                        # канонизация профиля (intent/login/редиректы → https://x.com/<handle>)
                        prof = _extract_x_profile(
                            vv
                        ) or _resolve_x_profile_via_redirect(vv)
                        vv = prof or ""
                    j_clean[k] = vv
                elif isinstance(v, list):
                    j_clean[k] = [
                        force_https(_abs(base_url, x))
                        for x in v
                        if isinstance(x, str) and x
                    ]

            # валидация twitterURL: должен указывать на x.com/twitter.com
            if j_clean.get("twitterURL"):
                prof = _extract_x_profile(
                    j_clean["twitterURL"]
                ) or _resolve_x_profile_via_redirect(j_clean["twitterURL"])
                j_clean["twitterURL"] = prof or ""

            # если нет twitterURL, но есть twitterAll - возьмем первый валидный
            if (not j_clean.get("twitterURL")) and isinstance(
                j_clean.get("twitterAll"), list
            ):
                for u in j_clean["twitterAll"]:
                    if isinstance(u, str) and u:
                        prof = _extract_x_profile(u) or _resolve_x_profile_via_redirect(
                            u
                        )
                        if prof:
                            j_clean["twitterURL"] = prof
                            break

            # собираем финальный словарь links из j_clean (без html/text и прочего мусора)
            _init_keys = set(_SOCIAL_KEYS) | {"websiteURL", "documentURL"}
            links = {k: "" for k in _init_keys if k != "twitterAll"}
            links["websiteURL"] = base_url
            links["documentURL"] = ""
            twitter_all: list[str] = []

            for k in list(links.keys()) + ["documentURL"]:
                if k in j_clean and j_clean[k]:
                    links[k] = j_clean[k] if isinstance(j_clean[k], str) else j_clean[k]

            # нормализуем twitterAll
            if isinstance(j_clean.get("twitterAll"), list):
                for u in j_clean["twitterAll"]:
                    if isinstance(u, str) and u:
                        u2 = force_https(u.replace("twitter.com", "x.com"))
                        if u2 not in twitter_all:
                            twitter_all.append(u2)

            # если нет twitterURL - добьем из twitterAll
            if not links.get("twitterURL") and twitter_all:
                links["twitterURL"] = twitter_all[0]

            # донастройка twitter
            if links.get("twitterURL"):
                links["twitterURL"] = force_https(
                    links["twitterURL"].replace("twitter.com", "x.com")
                )

            # если discord пришел относительным/внутренним - разворачиваем до discord.gg/.com
            if links.get("discordURL") and not re.search(
                r"discord\.(gg|com)", links["discordURL"], re.I
            ):
                links["discordURL"] = force_https(_abs(base_url, links["discordURL"]))
                if not re.search(r"discord\.(gg|com)", links["discordURL"], re.I):
                    resolved = _resolve_external(
                        links["discordURL"], ("discord.gg", "discord.com")
                    )
                    if resolved:
                        links["discordURL"] = resolved

            # финальная абсолютная нормализация (на всякий случай)
            for k, v in list(links.items()):
                if isinstance(v, str) and v:
                    links[k] = force_https(_abs(base_url, v))

            if twitter_all:
                links["twitterAll"] = list(dict.fromkeys(twitter_all))

            return links
    except Exception:
        pass

    # обычный html → dom-парсинг зон
    soup = BeautifulSoup(html or "", "html.parser")
    _init_keys = set(_SOCIAL_KEYS) | {"websiteURL", "documentURL"}
    links = {k: "" for k in _init_keys if k != "twitterAll"}
    links["websiteURL"] = base_url
    links["documentURL"] = ""
    twitter_all: list[str] = []

    def _maybe_add_twitter(href_abs: str):
        try:
            prof = _extract_x_profile(href_abs)
            if prof and prof not in twitter_all:
                twitter_all.append(prof)
        except Exception:
            pass

    # основные зоны где чаще всего лежат соцссылки
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
            href = urljoin(base_url, a["href"])
            text = (a.get_text(" ", strip=True) or "").lower()
            rel = " ".join(a.get("rel") or []).lower()
            aria = (a.get("aria-label") or "").lower()

            for key, rx in _SOCIAL_PATTERNS.items():
                if links.get(key):
                    continue

                if key == "twitterURL":
                    # единая попытка извлечь профиль из href/редиректов/вложенных url
                    prof = _extract_x_profile(href) or _resolve_x_profile_via_redirect(
                        href
                    )
                    # если по href пусто, но текст/aria намекают на Twitter — еще одна попытка
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
                    # специальный случай: текст/aria явно говорят "discord", но href не на discord-домене (bit.ly и т.п.)
                    elif key == "discordURL" and (
                        "discord" in text or "discord" in aria
                    ):
                        links[key] = href

            prof_for_all = _extract_x_profile(href)
            if not prof_for_all:
                # если текст/aria явно указывает на Twitter/X, попробуем развернуть коротыш
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

    # если по зонам пусто - полный проход по ссылкам
    if all(not links[k] for k in links if k != "websiteURL"):
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            text = (a.get_text(" ", strip=True) or "").lower()
            aria = (a.get("aria-label") or "").lower()

            # доменная проверка для твиттера
            if not links["twitterURL"]:
                prof = _extract_x_profile(href)
                if not prof and (
                    ("twitter" in text) or ("x(" in text) or ("twitter" in aria)
                ):
                    prof = _resolve_x_profile_via_redirect(href)
                if prof:
                    links["twitterURL"] = prof

            for key, rx in _SOCIAL_PATTERNS.items():
                if key == "twitterURL":
                    continue
                if not links[key] and (
                    rx.search(href) or rx.search(text) or rx.search(aria)
                ):
                    links[key] = href
                elif (
                    key == "discordURL"
                    and not links[key]
                    and ("discord" in text or "discord" in aria)
                ):
                    links[key] = href

            # twitterAll - только канонический профиль
            prof_for_all = _extract_x_profile(href)
            if not prof_for_all and (
                ("twitter" in text) or ("x(" in text) or ("twitter" in aria)
            ):
                prof_for_all = _resolve_x_profile_via_redirect(href)
            if prof_for_all:
                _maybe_add_twitter(prof_for_all)

    if not links.get("twitterURL"):
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            text = (a.get_text(" ", strip=True) or "").lower()
            aria = (a.get("aria-label") or "").lower()
            prof = _extract_x_profile(href) or _resolve_x_profile_via_redirect(href)
            if (not prof) and (
                ("twitter" in text) or ("x(" in text) or ("twitter" in aria)
            ):
                prof = _resolve_x_profile_via_redirect(href)
            if prof:
                links["twitterURL"] = prof
                break

    # абсолютный фолбэк по сырому html (если нет <a>, но URL есть в тексте)
    if not links.get("twitterURL"):
        prof = _extract_x_profile_from_text(html)
        if prof:
            links["twitterURL"] = prof

    # документация (лучшая ссылка)
    doc = find_best_docs_link(soup, base_url)
    links["documentURL"] = doc or ""

    # если это главная и отсутствует хотя бы один из ключевых (twitter/discord) - дозаполняем из браузера
    if is_main_page and (not links.get("twitterURL") or not links.get("discordURL")):
        logger.info("browser_fetch socials partial fill: %s", base_url)
        payload = fetch_url_html_playwright(
            base_url, timeout=60, wait="networkidle", mode="socials"
        )
        try:
            j2 = json.loads(payload or "") or {}
        except Exception:
            j2 = {}

        if isinstance(j2, dict) and j2.get("websiteURL"):
            # оставить только разрешённые ключи
            allowed = set(_SOCIAL_KEYS) | {"twitterAll"}
            j2 = {k: v for k, v in j2.items() if k in allowed}

            # нормализация значений + абсолютные
            for k, v in list(j2.items()):
                if isinstance(v, str) and v:
                    vv = force_https(_abs(base_url, v))
                    if k == "twitterURL" and "twitter.com" in vv:
                        vv = vv.replace("twitter.com", "x.com")
                    j2[k] = vv
                elif isinstance(v, list):
                    j2[k] = [
                        force_https(_abs(base_url, x))
                        for x in v
                        if isinstance(x, str) and x
                    ]

            # валидация/нормализация twitter профиля
            if j2.get("twitterURL"):
                prof = _extract_x_profile(
                    j2["twitterURL"]
                ) or _resolve_x_profile_via_redirect(j2["twitterURL"])
                j2["twitterURL"] = prof or ""

            # дозаполняем только пустые поля
            for k in list(links.keys()):
                if k in j2 and j2[k] and not links.get(k):
                    if k == "twitterURL":
                        # страхуемся: записываем только канонический профиль
                        prof = _extract_x_profile(
                            j2[k]
                        ) or _resolve_x_profile_via_redirect(j2[k])
                        if prof:
                            links[k] = prof
                    else:
                        links[k] = j2[k]

            # добавим twitterAll, если пришёл
            if isinstance(j2.get("twitterAll"), list) and j2["twitterAll"]:
                links["twitterAll"] = list(
                    dict.fromkeys(
                        [
                            force_https(u.replace("twitter.com", "x.com"))
                            for u in j2["twitterAll"]
                            if isinstance(u, str) and u
                        ]
                    )
                )

            # финальная нормализация
            for k, v in list(links.items()):
                if isinstance(v, str) and v:
                    if k == "twitterURL":
                        v = _extract_x_profile(v) or ""
                    links[k] = force_https(_abs(base_url, v)) if v else ""

    # fallback для главной: попросим браузерный парсер соцсетей (если совсем пусто)
    if is_main_page and all(
        not links[k] for k in links if k not in ("websiteURL", "documentURL")
    ):
        logger.info("browser_fetch socials fallback: %s", base_url)
        payload = fetch_url_html_playwright(
            base_url, timeout=60, wait="networkidle", mode="socials"
        )
        try:
            j2 = json.loads(payload or "") or {}
        except Exception:
            j2 = {}

        # если пришел соц-JSON - аккуратно смержим только разрешенные поля
        if isinstance(j2, dict) and j2.get("websiteURL"):
            allowed = set(_SOCIAL_KEYS)
            for k in list(j2.keys()):
                if k not in allowed:
                    j2.pop(k, None)

            # нормализация значений
            for k, v in list(j2.items()):
                if isinstance(v, str) and v:
                    vv = force_https(_abs(base_url, v))
                    if k == "twitterURL" and "twitter.com" in vv:
                        vv = vv.replace("twitter.com", "x.com")
                    j2[k] = vv

            # валидация twitterURL
            if j2.get("twitterURL"):
                if not re.search(
                    r"(?:^https?://)?(?:www\.)?(?:x\.com|twitter\.com)/",
                    j2["twitterURL"],
                    re.I,
                ):
                    j2["twitterURL"] = ""

            # мержим найденные соцсети в текущий словарь links (пустые не перетираем)
            for k in list(links.keys()):
                if k in j2 and j2[k] and not links.get(k):
                    if k == "twitterURL":
                        prof = _extract_x_profile(
                            j2[k]
                        ) or _resolve_x_profile_via_redirect(j2[k])
                        if prof:
                            links[k] = prof
                    else:
                        links[k] = j2[k]

            # домержим twitterAll
            if isinstance(j2.get("twitterAll"), list):
                for u in j2["twitterAll"]:
                    if isinstance(u, str) and u:
                        u2 = force_https(u.replace("twitter.com", "x.com"))
                        if u2 not in twitter_all:
                            twitter_all.append(u2)

            # попробуем вычислить документацию по отрендеренному dom, если он был в payload
            html2 = ""
            try:
                tmp = json.loads(payload or "") if payload else {}
                if isinstance(tmp, dict):
                    html2 = tmp.get("html") or tmp.get("text") or ""
            except Exception:
                html2 = ""

            if html2:
                soup2 = BeautifulSoup(html2, "html.parser")
                doc2 = find_best_docs_link(soup2, base_url)
                if doc2 and not links.get("documentURL"):
                    links["documentURL"] = doc2

            # если нет twitterURL - добем из twitterAll
            if (not links.get("twitterURL")) and twitter_all:
                links["twitterURL"] = twitter_all[0]

            # финальная нормализация и возврат
            for k, v in list(links.items()):
                if isinstance(v, str) and v:
                    if k == "twitterURL":
                        v = v.replace("twitter.com", "x.com")
                    links[k] = force_https(_abs(base_url, v))
            if twitter_all:
                links["twitterAll"] = list(dict.fromkeys(twitter_all))
            return links

        # если прилетел не соц-json, но есть html/text - второй круг по dom из браузера
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
                        href = urljoin(base_url, a["href"])
                        text = (a.get_text(" ", strip=True) or "").lower()
                        rel = " ".join(a.get("rel") or []).lower()
                        aria = (a.get("aria-label") or "").lower()

                        for key, rx in _SOCIAL_PATTERNS.items():
                            if not links[key] and (
                                rx.search(href)
                                or rx.search(text)
                                or rx.search(rel)
                                or rx.search(aria)
                            ):
                                if key == "twitterURL":
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

                        # twitterAll - строго через профиль/редирект
                        prof_for_all = _extract_x_profile(href)
                        if not prof_for_all and (
                            ("twitter" in text) or ("x(" in text) or ("twitter" in aria)
                        ):
                            prof_for_all = _resolve_x_profile_via_redirect(href)
                        if prof_for_all:
                            _maybe_add_twitter(prof_for_all)

                for z in zones2:
                    _scan2(z)

                if all(
                    not links[k]
                    for k in links
                    if k not in ("websiteURL", "documentURL")
                ):
                    for a in soup2.find_all("a", href=True):
                        href = urljoin(base_url, a["href"])
                        text = (a.get_text(" ", strip=True) or "").lower()
                        rel = " ".join(a.get("rel") or []).lower()
                        aria = (a.get("aria-label") or "").lower()

                        for key, rx in _SOCIAL_PATTERNS.items():
                            if key == "twitterURL":
                                continue
                            if not links[key] and (
                                rx.search(href)
                                or rx.search(text)
                                or rx.search(rel)
                                or rx.search(aria)
                            ):
                                links[key] = href
                            elif (
                                key == "discordURL"
                                and not links[key]
                                and ("discord" in text or "discord" in aria)
                            ):
                                links[key] = href

                        # просто соберем кандидатов в twitterAll, но не присваиваем twitterURL
                        if _SOCIAL_PATTERNS["twitterURL"].search(href):
                            _maybe_add_twitter(href)

                # если после сканирования зон twitterURL не найден - пройдем весь dom только под twitter
                if not links.get("twitterURL"):
                    for a in soup2.find_all("a", href=True):
                        href = urljoin(base_url, a["href"])
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
                            links["twitterURL"] = prof
                            break

                # повторно docs по рендеренному dom
                doc2 = find_best_docs_link(soup2, base_url)
                if doc2 and not links.get("documentURL"):
                    links["documentURL"] = doc2

            # фолбэк: искать прямые профили X/Twitter в сыром HTML
            if not links.get("twitterURL"):
                m = re.search(
                    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,15})(?:[/?#]|\b)",
                    html or "",
                    flags=re.I,
                )
                if m:
                    links["twitterURL"] = f"https://x.com/{m.group(1)}"
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
                                links["twitterURL"] = prof
                                break

            # финальная нормализация - все в https, twitter → x.com
            if links.get("twitterURL"):
                prof = _extract_x_profile(links["twitterURL"])
                if not prof:
                    logger.warning(
                        "web: drop invalid twitterURL: %s", links["twitterURL"]
                    )
                    links["twitterURL"] = ""
                else:
                    links["twitterURL"] = prof

    # финальный проход: абсолютный URL + https для всех строк
    for k, v in list(links.items()):
        if isinstance(v, str) and v:
            vv = v if v.startswith("http") else urljoin(base_url, v)
            vv = force_https(vv)
            if k == "twitterURL":
                vv = _extract_x_profile(vv) or _resolve_x_profile_via_redirect(vv) or ""
            links[k] = vv

    # если discord остался внутренним путем - разворачиваем
    if links.get("discordURL") and not re.search(
        r"discord\.(gg|com)", links["discordURL"], re.I
    ):
        links["discordURL"] = force_https(_abs(base_url, links["discordURL"]))
        if not re.search(r"discord\.(gg|com)", links["discordURL"], re.I):
            resolved = _resolve_external(
                links["discordURL"], ("discord.gg", "discord.com")
            )
            if resolved:
                links["discordURL"] = resolved

    # добавим twitterAll, если собрали несколько профилей на сайте
    if twitter_all:
        links["twitterAll"] = list(dict.fromkeys(twitter_all))

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
