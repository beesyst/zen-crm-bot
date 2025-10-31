from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


# Привод URL к https (и очистка пустых/невалидных значений)
def force_https(url: str | None) -> str:
    if not url or not isinstance(url, str):
        return ""
    u = url.strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.lower().startswith("http://"):
        return "https://" + u[7:]
    return u


# Уборка трекинговых параметров из query (?utm_*, fbclid, gclid и т.д.)
def _strip_tracking_params(u: str) -> str:
    try:
        p = urlparse(u)
        qs = []
        for k, v in parse_qsl(p.query, keep_blank_values=True):
            if re.match(r"^(utm_|mc_)", k, re.I):
                continue
            if k.lower() in {
                "fbclid",
                "gclid",
                "yclid",
                "twclid",
                "dclid",
                "ref",
                "ref_",
                "refsrc",
                "ref_src",
                "source",
                "src",
                "aff",
                "affiliate",
                "campaign",
                "utm",
                "igshid",
            }:
                continue
            qs.append((k, v))
        clean = p._replace(query=urlencode(qs))
        return urlunparse(clean)
    except Exception:
        return u


# Нормализация единого URL (https + без трекинга + без завершающего слеша)
def normalize_url(u: str | None) -> str:
    if not isinstance(u, str) or not u.strip():
        return ""
    s = force_https(u.strip())
    if not s:
        return ""
    s = _strip_tracking_params(s)
    s = twitter_to_x(s)

    # спец-правило для medium: оставляем только корень публикации/организации
    try:
        p = urlparse(s)
        host = (p.netloc or "").lower().replace("www.", "")
        if host.endswith("medium.com"):
            # /pub/slug... -> /pub
            # /@user/slug... -> /@user
            parts = [seg for seg in (p.path or "/").split("/") if seg]
            keep = ("/" + parts[0]) if parts else "/"
            s = urlunparse(p._replace(path=keep, params="", query="", fragment=""))
    except Exception:
        pass

    return s.rstrip("/")


# Нормализация списка URL (с сохранением порядка и дедупликацией)
def normalize_urls_list(lst: list[str] | None) -> list[str]:
    out: list[str] = []
    seen = set()
    for x in lst or []:
        u = normalize_url(x)
        if u and u not in seen:
            out.append(u)
            seen.add(u)
    return out


# Нормализация хоста/домена (lower, без www., без порта/пути)
def normalize_host(h: str | None) -> str:
    s = (h or "").strip().lower()
    if not s:
        return ""
    if s.startswith("//"):
        s = "https:" + s
    if s.startswith("http://") or s.startswith("https://"):
        try:
            s = urlparse(s).netloc
        except Exception:
            s = s.split("://", 1)[-1]
    s = s.split("/", 1)[0]
    s = s.split(":", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    return s


# Нормализация списка доменов/хостов (с сохранением порядка и дедупликацией)
def normalize_host_list(lst: list[str] | None) -> list[str]:
    out: list[str] = []
    seen = set()
    for x in lst or []:
        h = normalize_host(x)
        if h and h not in seen:
            out.append(h)
            seen.add(h)
    return out


# Приведение twitter.com → x.com
def twitter_to_x(u: str | None) -> str:
    if not isinstance(u, str) or not u.strip():
        return ""
    s = force_https(u.strip())
    if not s:
        return ""

    # если это статус/любая вложенная страница (включая /status/, /i/...), ничего не меняем
    try:
        p = urlparse(s)
        host = (p.netloc or "").lower().replace("www.", "")
        path = p.path or "/"
        if host in {"twitter.com", "x.com"}:
            # статусные и служебные пути оставляем как есть
            if re.search(r"/status/\d+", path, re.I) or re.match(
                r"^/i/(\b|/)", path, re.I
            ):
                return s.rstrip("/")
            # канонизируем только ЧИСТЫЙ профиль /<handle>
            m = re.match(r"^/([A-Za-z0-9_]{1,15})/?$", path)
            if m:
                return f"https://x.com/{m.group(1)}"
    except Exception:
        pass

    # intent/follow?screen_name=<handle> → профиль
    m = re.match(
        r"^https://(?:www\.)?twitter\.com/(?:intent/follow|intent/user)\b", s, re.I
    )
    if m:
        from urllib.parse import parse_qs, urlparse

        try:
            qs = parse_qs(urlparse(s).query or "")
            screen = (qs.get("screen_name") or [""])[0].strip()
            if screen and re.match(r"^[A-Za-z0-9_]{1,15}$", screen):
                return f"https://x.com/{screen}"
        except Exception:
            pass

    # i/flow/login?redirect_after_login=... → не трогаем статусные ссылки, профиль - канонизируем
    if "redirect_after_login" in s:
        from urllib.parse import parse_qs, unquote, urlparse

        try:
            qs = parse_qs(urlparse(s).query or "")
            redir = (qs.get("redirect_after_login") or [""])[0]
            if redir:
                redir = force_https(unquote(redir))
                pp = urlparse(redir)
                if re.search(r"/status/\d+", pp.path or "", re.I):
                    return s.rstrip("/")
                m2 = re.match(
                    r"^https://(?:www\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,15})/?$",
                    redir,
                    re.I,
                )
                if m2:
                    return f"https://x.com/{m2.group(1)}"
                m3 = re.match(r"^/([A-Za-z0-9_]{1,15})/?$", redir)
                if m3:
                    return f"https://x.com/{m3.group(1)}"
        except Exception:
            pass

    # generic ?url|u|to|target|redirect|redirect_uri=<twitter/x профиль>
    if "?" in s:
        from urllib.parse import parse_qs, unquote, urlparse

        try:
            qs = parse_qs(urlparse(s).query or "")
            for key in ("url", "u", "to", "target", "redirect", "redirect_uri"):
                for cand in qs.get(key, []):
                    cand = force_https(unquote(cand or ""))
                    # статусные не трогаем
                    pp = urlparse(cand)
                    if re.search(r"/status/\d+", (pp.path or ""), re.I):
                        return s.rstrip("/")
                    m4 = re.match(
                        r"^https://(?:www\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,15})/?$",
                        cand,
                        re.I,
                    )
                    if m4:
                        return f"https://x.com/{m4.group(1)}"
        except Exception:
            pass

    return s.rstrip("/")


# Хелпер для списков твиттер-URL
def twitter_list_to_x(urls: list[str] | None) -> list[str]:
    out: list[str] = []
    seen = set()
    for u in urls or []:
        nu = twitter_to_x(u)
        if nu and re.match(r"^https://(?:www\.)?x\.com/[A-Za-z0-9_]{1,15}$", nu, re.I):
            if nu not in seen:
                out.append(nu)
                seen.add(nu)
    return out


# Нормализация словаря соц-ссылок: https + уборка трекинга + трим/слэш
def normalize_socials(socials: dict | None) -> dict:
    if not isinstance(socials, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in socials.items():
        out[k] = normalize_url(v) if isinstance(v, str) and v.strip() else ""
    return out


# Парс бренд-токена из домена (первый сегмент без www и лишних символов)
def brand_from_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = urlparse(force_https(url)).netloc.lower()
        host = host.replace("www.", "")
        token = host.split(".")[0]
        token = re.sub(r"[^a-z0-9\-]+", "", token)
        return token
    except Exception:
        return ""


# Чистка человекочитаемого имени проекта (обрезка хвостов, пробелов, длины)
def clean_project_name(s: str | None) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(
        r"\b(official site|official|homepage|home)\b$", "", s, flags=re.I
    ).strip()
    if len(s) > 80:
        s = s[:80].rstrip()
    return s


# Быстрая эвристика плохих имен (слишком короткие/технические)
def is_bad_name(s: str | None) -> bool:
    if not s:
        return True
    bad = {"home", "homepage", "official", "docs", "documentation", "index"}
    low = s.strip().lower()
    return (not low) or (low in bad) or len(low) < 2
