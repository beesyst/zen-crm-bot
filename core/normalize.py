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
        qs = [
            (k, v)
            for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not re.match(r"^(utm_|fbclid|gclid|yclid|mc_)", k, re.I)
        ]
        clean = p._replace(query=urlencode(qs))
        return urlunparse(clean)
    except Exception:
        return u


# Нормализация словаря соц-ссылок: https + уборка трекинга + трим/слэш
def normalize_socials(socials: dict | None) -> dict:
    if not isinstance(socials, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in socials.items():
        if isinstance(v, str) and v.strip():
            u = force_https(v.strip())
            u = _strip_tracking_params(u)
            out[k] = u.rstrip("/")
        else:
            out[k] = ""
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
