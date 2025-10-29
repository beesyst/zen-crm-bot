from __future__ import annotations

from typing import Any, Dict

from core.settings import get_settings


def get_proxy_cfg() -> Dict[str, Any]:
    s = get_settings()
    px = (s.get("parser") or {}).get("proxy") or {}
    return {
        "scheme": str(px.get("scheme") or "").lower(),
        "host": str(px.get("host") or ""),
        "port": int(px.get("port") or 0),
        "username": str(px.get("username") or ""),
        "password": str(px.get("password") or ""),
    }


def enabled(px: Dict[str, Any] | None = None) -> bool:
    p = px or get_proxy_cfg()
    return bool(p.get("scheme") and p.get("host") and p.get("port"))


def as_requests_proxies(px: Dict[str, Any] | None = None) -> Dict[str, str]:
    p = px or get_proxy_cfg()
    if not enabled(p):
        return {}
    auth = (
        f"{p['username']}:{p['password']}@"
        if p.get("username") and p.get("password")
        else ""
    )
    url = f"{p['scheme']}://{auth}{p['host']}:{p['port']}"
    return {"http": url, "https": url}


def as_playwright_json(px: Dict[str, Any] | None = None) -> Dict[str, Any]:
    p = px or get_proxy_cfg()
    if not enabled(p):
        return {}
    out: Dict[str, Any] = {"server": f"{p['scheme']}://{p['host']}:{p['port']}"}
    if p.get("username"):
        out["username"] = p["username"]
    if p.get("password"):
        out["password"] = p["password"]
    return out
