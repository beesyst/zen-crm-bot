from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_setup import get_logger
from core.normalize import force_https, normalize_url, twitter_to_x
from core.settings import (
    get_contact_roles,
    get_link_collections,
    get_social_hosts,
    get_social_keys,
)

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
        if not isinstance(v, str) or not v:
            continue
        vv = normalize_url(v)
        if k == "twitter":
            vv = twitter_to_x(vv)
        out[k] = vv
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


# Словарь соц-ссылок + website
def extract_socials_from_aggregator(agg_url: str) -> dict:
    keys = list(dict.fromkeys([*get_social_keys(), "website"]))
    out = {k: "" for k in keys}

    html = _fetch_html(agg_url)
    if not html:
        return out

    soup = BeautifulSoup(html, "html.parser")
    base_host = _host(agg_url)

    candidate_sites: list[str] = []

    # выдергиваем целевой URL из типовых редиректоров агрегаторов (?url=, ?u=, ?to=, ?target=, ?redirect=, ?redirect_uri=)
    def _unwrap_redirect(u: str) -> str:
        try:
            from urllib.parse import parse_qs, unquote, urlparse

            p = urlparse(u)
            qs = parse_qs(p.query or "")
            for key in ("url", "u", "to", "target", "redirect", "redirect_uri"):
                for cand in qs.get(key, []):
                    cand = force_https(unquote(cand or ""))
                    if cand:
                        return cand
        except Exception:
            pass
        return u

    def _emit(href: str):
        raw = urljoin(agg_url, href)
        # сначала разворачиваем возможный редиректор агрегатора
        raw = _unwrap_redirect(raw)
        u = normalize_url(raw)  # внутри вызовется twitter_to_x()
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
            if "twitter" in out and not out["twitter"]:
                out["twitter"] = twitter_to_x(u)
            return
        if h in ("t.me", "telegram.me"):
            if "telegram" in out and not out["telegram"]:
                out["telegram"] = u
            return
        if (
            h in ("discord.gg", "discord.com")
            or h.endswith(".discord.gg")
            or h.endswith(".discord.com")
        ):
            if "discord" in out and not out["discord"]:
                out["discord"] = u
            return
        if h in ("youtube.com", "youtu.be") or h.endswith(".youtube.com"):
            if "youtube" in out and not out["youtube"]:
                out["youtube"] = u
            return
        if h in ("linkedin.com", "lnkd.in") or h.endswith(".linkedin.com"):
            if "linkedin" in out and not out["linkedin"]:
                out["linkedin"] = u
            return
        if h == "reddit.com" or h.endswith(".reddit.com"):
            if "reddit" in out and not out["reddit"]:
                out["reddit"] = u
            return
        if h == "medium.com" or h.endswith(".medium.com"):
            if "medium" in out and not out["medium"]:
                out["medium"] = u
            return
        if h == "github.com" or h.endswith(".github.com"):
            if "github" in out and not out["github"]:
                out["github"] = u
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

    # выберем лучший website
    def _score(u: str) -> tuple[int, int]:
        try:
            p = urlparse(u)
            host = (p.netloc or "").lower().replace("www.", "")
            path = (p.path or "/").strip("/")
            # penalty за субдомен (docs/blog/help/wiki и любые поддомены)
            sub_penalty = 0
            if "." in host:
                sub_penalty = 2
            if host.startswith(("docs.", "blog.", "help.", "wiki.")):
                sub_penalty = 3
            # глубина пути
            depth = 0 if not path else len([s for s in path.split("/") if s])
            return (sub_penalty, depth)
        except Exception:
            return (9, 9)

    if candidate_sites:
        uniq = []
        seen = set()
        for u in candidate_sites:
            uu = normalize_url(u)
            if uu and uu not in seen:
                uniq.append(uu)
                seen.add(uu)
        uniq.sort(key=_score)
        out["website"] = uniq[0]

    # финальная нормализация
    return _normalize_socials_dict(out)


# Контакты (email + persons) из агрегатора по конфигу roles
def extract_contacts_from_aggregator(agg_url: str) -> dict:
    html = _fetch_html(agg_url)
    res = {"emails": [], "forms": [], "persons": []}
    if not html:
        logger.info(
            "Агрегатор контакты %s: %s",
            agg_url,
            {"emails": res.get("emails", []), "persons": res.get("persons", [])},
        )
        return res

    soup = BeautifulSoup(html, "html.parser")
    roles_map = get_contact_roles()

    EMAIL_RX = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)

    def _resolve_role(text_or_href: str) -> str:
        s = (text_or_href or "").lower()
        for role, tokens in roles_map.items():
            if any(tok in s for tok in tokens):
                return role
        return ""

    def _mk_person(name: str, role: str, **channels):
        person = {"name": name, "role": role, "source": _host(agg_url)}
        for k, v in channels.items():
            if v:
                person[k] = normalize_url(v)
        return person

    # email (mailto / текст)
    found_email = ""
    for a in soup.find_all("a", href=True):
        href = a["href"] or ""
        if href.lower().startswith("mailto:"):
            mail = href.split(":", 1)[-1].strip()
            if EMAIL_RX.fullmatch(mail):
                found_email = mail
                break
    if not found_email:
        m = EMAIL_RX.search(soup.get_text(" ", strip=True) or "")
        if m:
            found_email = m.group(0)
    if found_email:
        res["emails"].append(found_email)

    # персональные каналы по ролям
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True) or ""
        href_abs = normalize_url(urljoin(agg_url, a["href"]))
        h = _host(href_abs)
        role = _resolve_role(text) or _resolve_role(href_abs)

        if not role:
            continue

        # подобрать имя
        name = "Support" if role == "support" else (text.strip() or role.title())

        if h in ("t.me", "telegram.me"):
            res["persons"].append(_mk_person(name, role, telegram=href_abs))
        elif h in ("discord.gg", "discord.com"):
            res["persons"].append(_mk_person(name, role, discord=href_abs))
        elif h in ("x.com", "twitter.com"):
            res["persons"].append(_mk_person(name, role, x=twitter_to_x(href_abs)))
        elif h in ("linkedin.com", "lnkd.in"):
            res["persons"].append(_mk_person(name, role, linkedin=href_abs))
        else:
            # общий сайт/форма контакта
            if href_abs.startswith("http"):
                res["persons"].append(_mk_person(name, role, website=href_abs))

    # дедуп по (role, основной канал)
    seen = set()
    deduped = []

    def _key(p):
        return (
            (p.get("role") or "").lower(),
            (
                p.get("email")
                or p.get("telegram")
                or p.get("discord")
                or p.get("linkedin")
                or p.get("x")
                or p.get("website")
                or ""
            ).lower(),
        )

    for p in res["persons"]:
        k = _key(p)
        if k and k not in seen:
            seen.add(k)
            deduped.append(p)
    res["persons"] = deduped

    return res


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
