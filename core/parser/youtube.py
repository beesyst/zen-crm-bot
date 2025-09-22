from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import requests
from core.normalize import force_https
from core.settings import get_http_ua

UA = get_http_ua()


# Преобразование обычных youtube-ссылок (watch?v=... / youtu.be/ID) в embed-URL
def youtube_watch_to_embed(url: str | None) -> str:
    url = force_https(url or "")
    if not url:
        return ""
    try:
        p = urlparse(url)
        if "youtube.com" in p.netloc and p.path == "/watch":
            vid = parse_qs(p.query).get("v", [""])[0]
            return f"https://www.youtube.com/embed/{vid}" if vid else ""
        if "youtu.be" in p.netloc and p.path.strip("/"):
            vid = p.path.strip("/")
            return f"https://www.youtube.com/embed/{vid}"
    except Exception:
        pass
    return ""


# Парс YouTube-хэндл в формате "@handle" из URL канала
def youtube_to_handle(url: str | None) -> str:
    url = force_https(url or "")
    if not url:
        return ""
    try:
        p = urlparse(url)
        if "youtube.com" in p.netloc and p.path.startswith("/@"):
            return p.path.lstrip("/")
    except Exception:
        pass
    return ""


# Получение заголовка видео/канала через YouTube oEmbed (если доступно)
def youtube_oembed_title(url: str | None) -> str:
    url = force_https(url or "")
    if not url:
        return ""
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=12,
            headers={"User-Agent": UA},
        )
        if r.status_code == 200:
            data = r.json()
            return (data.get("title") or "").strip()
    except Exception:
        pass
    return ""
