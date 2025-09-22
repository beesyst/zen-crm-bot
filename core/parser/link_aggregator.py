from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from core.log_setup import get_logger
from core.normalize import force_https, normalize_url, twitter_to_x
from core.settings import (
    get_contact_roles,
    get_http_ua,
    get_link_collections,
    get_social_host_map,
    get_social_keys,
)

logger = get_logger("link_aggregator")
UA = get_http_ua()


# Хелпер: вернуть netloc без www
def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# Хелпер: получить список доменов агрегаторов из конфига (обязательно)
def _get_domains() -> list[str]:
    domains = get_link_collections()
    if not domains:
        raise RuntimeError(
            "config/settings.yml: 'link_collections' обязателен и не может быть пустым"
        )
    return domains


# Проверка: URL принадлежит одному из доменов агрегаторов (включая поддомены)
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


# Хелпер: URL принадлежит соц-домену из host_map (включая поддомены)
def _is_social_host(h: str) -> bool:
    h = (h or "").lower()
    host_map = get_social_host_map()
    # прямое совпадение
    if h in host_map:
        return True
    # совпадение по поддомену
    return any(h.endswith("." + base) for base in host_map.keys())


# Хелпер: нормализовать словарь соц-ссылок
def _normalize_socials_dict(d: dict) -> dict:
    allowed = set(get_social_keys())
    out = {}
    for k, v in (d or {}).items():
        if not isinstance(v, str) or not v:
            continue
        vv = normalize_url(v)
        if k == "twitter":
            vv = twitter_to_x(vv)
        if k in allowed:
            out[k] = vv
    return out


# Простой in-memory кэш HTML по URL агрегатора
_HTML_CACHE: dict[str, str] = {}


# Загрузка HTML агрегатора с кэшем
def _fetch_html(url: str, timeout: int = 20) -> str:
    u = force_https(url)
    if u in _HTML_CACHE:
        return _HTML_CACHE[u]
    try:
        resp = requests.get(u, timeout=timeout, headers={"User-Agent": UA})
        html = resp.text or ""
    except Exception as e:
        logger.warning("Aggregator request failed: %s (%s)", u, e)
        html = ""
    _HTML_CACHE[u] = html
    return html


# Извлечь соц-ссылки (по ключам из конфига) и официальный сайт с агрегатора
def extract_socials_from_aggregator(agg_url: str) -> dict:
    # набор ключей: конфиг-ключи + обязательный website
    keys = list(dict.fromkeys([*get_social_keys(), "website"]))
    out = {k: "" for k in keys}

    html = _fetch_html(agg_url)
    if not html:
        return out

    soup = BeautifulSoup(html, "html.parser")
    base_host = _host(agg_url)
    host_map = get_social_host_map()

    candidate_sites: list[str] = []

    # Хелпер: разворачиваем типовые редиректорные параметры агрегаторов
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

    # Хелпер: реверсия host->ключ через host_map (учитывая поддомены)
    def _host_to_key(h: str) -> str | None:
        if h in host_map:
            return host_map[h]
        for base, k in host_map.items():
            if h.endswith("." + base):
                return k
        return None

    # Обработка одной найденной ссылки
    def _emit(href: str):
        raw = urljoin(agg_url, href)
        # разворачиваем возможный редиректор агрегатора
        raw = _unwrap_redirect(raw)
        u = normalize_url(raw)
        if not u:
            return

        h = _host(u)
        if not h:
            return

        # попадает в известные соц-домен(ы) → пишем в соответствующий конфиг-ключ
        social_key = _host_to_key(h)
        if social_key and social_key in out and not out[social_key]:
            out[social_key] = twitter_to_x(u) if social_key == "twitter" else u
            return

        # кандидаты на официальный сайт (не сам агрегатор и не соц-хосты)
        if re.match(r"^https?://", u) and (not _is_social_host(h)) and (h != base_host):
            candidate_sites.append(u)

    # парсим <a>
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

    # Выбираем лучший website среди кандидатов
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

    return _normalize_socials_dict(out)


# Извлечь контакты (email, формы, люди) с агрегатора по картам ролей из конфига
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
    host_map = get_social_host_map()
    allowed_keys = set(get_social_keys())

    EMAIL_RX = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)

    # определяем роль по токенам из конфига
    def _resolve_role(text_or_href: str) -> str:
        s = (text_or_href or "").lower()
        for role, tokens in roles_map.items():
            if any(tok in s for tok in tokens):
                return role
        return ""

    # создаем запись о человеке с нормализованными каналами
    def _mk_person(name: str, role: str, **channels):
        person = {"name": name, "role": role, "source": _host(agg_url)}
        for k, v in channels.items():
            if v:
                url_norm = normalize_url(v)
                if k == "twitter":
                    url_norm = twitter_to_x(url_norm)
                # пишем только разрешённые ключи
                if k in allowed_keys:
                    person[k] = url_norm
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

    # реверсия host->social key с учетом поддоменов
    def _host_to_key(h: str) -> str | None:
        if h in host_map:
            return host_map[h]
        for base, k in host_map.items():
            if h.endswith("." + base):
                return k
        return None

    # персональные каналы по ролям
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True) or ""
        href_abs = normalize_url(urljoin(agg_url, a["href"]))
        h = _host(href_abs)
        role = _resolve_role(text) or _resolve_role(href_abs)
        if not role:
            continue

        # имя по умолчанию
        name = "Support" if role == "support" else (text.strip() or role.title())

        # определить ключ соцсети по host_map
        social_key = _host_to_key(h)

        if social_key and social_key in allowed_keys:
            # twitter → x.com
            channel_url = (
                twitter_to_x(href_abs) if social_key == "twitter" else href_abs
            )
            res["persons"].append(_mk_person(name, role, **{social_key: channel_url}))
        else:
            # общий сайт/форма контакта
            if href_abs.startswith("http"):
                res["persons"].append(_mk_person(name, role, website=href_abs))

    # дедуп по (role, основной канал)
    seen = set()
    deduped = []

    def _key(p: dict) -> tuple[str, str]:
        role = (p.get("role") or "").lower()
        # приоритет основного канала: email → конфиг-ключи → website
        main = p.get("email") or ""
        if not main:
            for k in get_social_keys():
                if p.get(k):
                    main = p.get(k)
                    break
        if not main:
            main = p.get("website") or ""
        return role, (main or "").lower()

    for p in res["persons"]:
        k = _key(p)
        if k and k not in seen:
            seen.add(k)
            deduped.append(p)
    res["persons"] = deduped

    return res


# Найти URL агрегаторов среди списка ссылок (с нормализацией и дедупом)
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


# Подтвердить, что агрегатор относится к проекту, и вернуть соц-ссылки
def verify_aggregator_belongs(
    agg_url: str, site_domain: str, handle: str | None
) -> tuple[bool, dict]:
    site_domain = (site_domain or "").lower().lstrip(".")
    html = _fetch_html(agg_url)
    if not html:
        return False, {}

    soup = BeautifulSoup(html, "html.parser")

    # проверка наличия офсайта по домену
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

    # проверка наличия ссылок именно на этот twitter/x-handle
    has_handle = False
    if handle:
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
            # мягкий фолбэк: по тексту страницы
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
