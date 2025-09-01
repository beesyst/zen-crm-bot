from __future__ import annotations

import re
from typing import Dict, List, Optional

import requests
from core.log_setup import get_logger

log = get_logger("host")

EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9\.-]+")
DISCORD_RE = re.compile(
    r"(?:https?://)?(?:discord\.gg|discord\.com/invite)/[A-Za-z0-9]+"
)
TG_RE = re.compile(r"(?:https?://)?t(?:elegram)?\.me/[A-Za-z0-9_]+")
FORM_RE = re.compile(r"<form[^>]*action=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)


def safe_fetch(url: str, timeout: int = 15) -> Optional[str]:
    try:
        r = requests.get(
            url, timeout=timeout, headers={"User-Agent": "zen-crm-ingest/1.0"}
        )
        if r.ok and r.text:
            return r.text
    except Exception as e:
        log.warning(
            "fetch failed",
            extra={"event": "ingest.fetch.failed", "url": url, "error": str(e)},
        )
    return None


def extract_all(html: str, base_url: str) -> Dict[str, List[str]]:
    emails = sorted(set(EMAIL_RE.findall(html)))
    discords = sorted(set(DISCORD_RE.findall(html)))
    tgs = sorted(set(TG_RE.findall(html)))
    forms = sorted(set(FORM_RE.findall(html)))
    return {"emails": emails, "discord": discords, "telegram": tgs, "forms": forms}


def ingest_from_website(website: str, project_name: str | None = None) -> Dict:
    if not website:
        return {
            "website": None,
            "emails": [],
            "discord": [],
            "telegram": [],
            "forms": [],
            "project_name": project_name or "Project",
        }
    html = safe_fetch(website)
    if not html:
        return {
            "website": website,
            "emails": [],
            "discord": [],
            "telegram": [],
            "forms": [],
            "project_name": project_name or "Project",
        }

    data = extract_all(html, website)
    log.info(
        "ingest.complete",
        extra={
            "event": "ingest.complete",
            "website": website,
            "counts": {k: len(v) for k, v in data.items()},
        },
    )
    data["website"] = website
    data["project_name"] = project_name or infer_project_name(html) or "Project"
    return data


def infer_project_name(html: str) -> Optional[str]:
    m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        return title[:80]
    return None
