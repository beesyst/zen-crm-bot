from __future__ import annotations

import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from core.log_setup import get_logger

logger = get_logger("contact")

# Локальный UA, чтобы не тянуть зависимость из web.py
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# Сбор контактов на сайте
def extract_contacts_from_site(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html or "", "html.parser")

    EMAIL_RX = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
    emails = set()

    # mailto
    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if href.lower().startswith("mailto:"):
            mail = href.split(":", 1)[-1].strip()
            if EMAIL_RX.fullmatch(mail):
                emails.add(mail)

    # email в тексте
    for m in EMAIL_RX.findall(soup.get_text(" ", strip=True) or ""):
        emails.add(m.strip())

    # формы/контакт страницы
    forms = set()
    for a in soup.find_all("a", href=True):
        text = (a.get_text(" ", strip=True) or "").lower()
        absu = urljoin(base_url, a["href"])
        if re.search(
            r"/(contact|support|help|customer|cs|ticket|request|submit|feedback)(?:/|$|\?)",
            absu,
            re.I,
        ) or any(
            k in text
            for k in (
                "contact",
                "support",
                "help",
                "customer service",
                "submit a request",
            )
        ):
            if absu.startswith("http"):
                forms.add(absu)

    out = {
        "emails": sorted(dict.fromkeys(e.lower() for e in emails)),
        "forms": sorted(dict.fromkeys(forms)),
        "persons": [],
    }
    logger.info(
        "Контакты на %s: %s", base_url, {"emails": out["emails"], "forms": out["forms"]}
    )
    return out


# Сбор контактов на GitHub
def extract_contacts_from_github(github_url: str, timeout: int = 20) -> dict:
    try:
        r = requests.get(github_url, timeout=timeout, headers={"User-Agent": UA})
        html = r.text or ""
    except Exception:
        html = ""

    soup = BeautifulSoup(html, "html.parser")
    EMAIL_RX = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
    emails = set()

    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if href.lower().startswith("mailto:"):
            mail = href.split(":", 1)[-1].strip()
            if EMAIL_RX.fullmatch(mail):
                emails.add(mail)

    for m in EMAIL_RX.findall(soup.get_text(" ", strip=True) or ""):
        emails.add(m.strip())

    out = {"emails": sorted(dict.fromkeys(e.lower() for e in emails))}
    logger.info("Контакты на %s: %s", github_url, out)
    return out
