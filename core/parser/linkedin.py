from __future__ import annotations

import base64
import json
import random
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from core.log_setup import get_logger
from core.paths import PROJECT_ROOT
from core.proxy import as_playwright_json, get_proxy_cfg
from core.proxy import enabled as proxy_enabled
from core.settings import get_linkedin_cfg

logger = get_logger("linkedin")

PLAYWRIGHT_JS = PROJECT_ROOT / "core" / "parser" / "playwright.js"


# Декодирование cookies из base64 (из продавца) в формат Playwright
def _decode_cookies_base64(b64: str) -> List[Dict[str, Any]]:
    if not b64 or not b64.strip():
        return []
    try:
        raw = base64.b64decode(b64.strip()).decode("utf-8", errors="ignore")
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        # некоторые селлеры заворачивают в {"cookies":[...]}
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            return data["cookies"]
    except Exception as e:
        logger.error("cookies_base64 decode failed: %s", e)
    return []


# Рандомная пауза между действиями (мс) на основе min/max из конфига
def _pick_action_delay_ms(throttle: Dict[str, int]) -> int:
    lo = int(throttle.get("min_action") or 1500)
    hi = int(throttle.get("max_action") or 3500)
    if lo < 0:
        lo = 0
    if hi < lo:
        hi = lo
    return random.randint(lo, hi)


# Построение аргументов CLI для вызова playwright.js
def _build_playwright_args(
    url: str, ua: str, cookies: List[Dict[str, Any]], cfg: Dict[str, Any]
) -> List[str]:
    args: List[str] = [
        "node",
        str(PLAYWRIGHT_JS),
        "--url",
        url,
        "--html",
        "--wait",
        "networkidle",
        "--timeout",
        "90000",
        "--ua",
        ua or "",
        "--waitSocialHosts",
        "linkedin.com,lnkd.in",
    ]

    if cookies:
        tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json")
        json.dump(cookies, tmp, ensure_ascii=False)
        tmp.flush()
        tmp.close()
        args.extend(["--cookies", Path(tmp.name).read_text(encoding="utf-8")])

    profile_dir = (cfg.get("persistent_profile") or "").strip()
    if profile_dir:
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        args.extend(["--profile", profile_dir])

    cookies_path = (cfg.get("cookies_path") or "").strip()
    if cookies_path:
        Path(cookies_path).parent.mkdir(parents=True, exist_ok=True)
        args.extend(["--cookiesPath", cookies_path])

    px_cfg = get_proxy_cfg()
    if proxy_enabled(px_cfg):
        args.extend(["--proxy", json.dumps(as_playwright_json(px_cfg))])

    return args


# Запуск playwright.js, возврат JSON-объекта результата
def _run_playwright(args: List[str]) -> Dict[str, Any]:
    try:
        logger.info("spawn: %s", " ".join(shlex.quote(a) for a in args))
        out = subprocess.check_output(args, cwd=str(PROJECT_ROOT), timeout=120)
        text = out.decode("utf-8", errors="ignore")
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except subprocess.CalledProcessError as e:
        payload = e.output.decode("utf-8", errors="ignore") if e.output else ""
        logger.error("playwright.js failed rc=%s out=%s", e.returncode, payload[:5000])
    except Exception as e:
        logger.exception("playwright.js error: %s", e)
    return {}


# Нормализация куки
def _normalize_cookies_for_playwright(
    src: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for c in src or []:
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "").strip()
        if not name:
            continue

        domain = str(c.get("domain") or "").strip()
        path = str(c.get("path") or "/").strip() or "/"

        # если домен отсутствует - ставим общий
        if not domain:
            domain = ".linkedin.com"

        # sameSite маппинг
        ss_raw = str(c.get("sameSite") or "").lower().strip()
        if ss_raw in ("no_restriction", "none"):
            sameSite = "None"
        elif ss_raw in ("lax",):
            sameSite = "Lax"
        elif ss_raw in ("strict",):
            sameSite = "Strict"
        else:
            sameSite = "None"

        # expires → seconds (Playwright ждёт epoch seconds int)
        exp = c.get("expires")
        if not exp:
            exp = c.get("expirationDate")
        try:
            expires = int(exp) if exp else -1
        except Exception:
            expires = -1

        out.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": bool(c.get("secure", True)),
                "httpOnly": bool(c.get("httpOnly", False)),
                "sameSite": sameSite,
                "expires": expires,
            }
        )
    return out


# Утилита: нормализация company/people URL
def _normalize_people_url(linkedin_company_url: str) -> str:
    u = (linkedin_company_url or "").strip()
    if not u:
        return ""
    u = re.sub(r"\?.*$", "", u).rstrip("/")  # убрать query и хвостовые /
    if "/people" in u:
        if not u.endswith("/people") and not u.endswith("/people/"):
            u = re.sub(r"/people/?$", "/people", u)  # унификация
        return u + ("" if u.endswith("/") else "/")
    if "/company/" in u:
        return u + "/people/"
    # неявный случай (вдруг дали домен) - возвращаем как есть
    return u if u.endswith("/") else (u + "/")


# Фильтрация по ролям с учетом простых нормализаций
def _role_matches(title: str, filters: List[str]) -> bool:
    low = (title or "").lower()
    for q in filters or []:
        qlow = (q or "").strip().lower()
        if qlow and qlow in low:
            return True
    return False


# Парсинг блока People (HTML) на имена/должности/ссылки на профили
def _parse_people_from_html(
    html: str,
    role_filters: List[str],
    limit: int,
) -> List[Dict[str, str]]:

    soup = BeautifulSoup(html or "", "html.parser")

    # надежнее искать ссылки на профили /in/<handle>
    anchors = soup.find_all("a", href=True)
    people: List[Dict[str, str]] = []
    seen_profiles = set()

    for a in anchors:
        href = (a.get("href") or "").strip()
        if not href:
            continue

        # находим ссылки на профили пользователей
        if re.search(r"/in/[^/?#]+", href):
            # восстанавливаем абсолютный URL
            try:
                from urllib.parse import urljoin

                absu = urljoin("https://www.linkedin.com/", href)
            except Exception:
                absu = href

            # эвристика: у ссылки родительский контейнер рядом с именем/титулом
            container = a
            for _ in range(3):
                if container and container.parent:
                    container = container.parent
            block_text = " ".join((container.get_text(" ", strip=True) or "").split())

            # имя - чаще всего выделено dir="ltr" или <span> рядом; если не нашли - берем начало блока
            name = ""
            name_node = container.find(
                lambda tag: tag.name in ("span", "div") and tag.get("dir") == "ltr"
            )
            if name_node:
                name = name_node.get_text(" ", strip=True)
            if not name:
                # запасной вариант: первая словесная подпоследовательность
                m = re.search(
                    r"([A-Z][a-zA-Z'’\-]+\s+[A-Z][a-zA-Z'’\-]+.*?)(?:\s{2,}|$)",
                    block_text,
                )
                name = (m.group(1).strip() if m else "").strip()

            # должность - часто рядом с именем или в следующем контейнере
            title = ""
            # примитивная эвристика по ключам
            m2 = re.search(
                r"(founder|co[-\s]?founder|head of|chief|lead|manager|director|business|partnership|bd|devrel|marketing|growth|community|sales)[^|•\n]*",
                block_text,
                re.I,
            )
            if m2:
                title = (m2.group(0) or "").strip()

            # фильтрация по ролям (если заданы)
            if role_filters and title and not _role_matches(title, role_filters):
                continue

            # дедуп по профилю
            key = absu.split("?")[0]
            if key in seen_profiles:
                continue
            seen_profiles.add(key)

            # формируем под наш people-шаблон: контакты пока пустые
            people.append(
                {
                    "name": name or "",
                    "title": title or "",
                    "linkedin": key,
                    "email": "",
                    "phone": "",
                    "twitter": "",
                    "telegram": "",
                    "discord": "",
                    "website": "",
                    "github": "",
                }
            )

            if limit and len(people) >= limit:
                break

    return people


# Высокоуровневая функция: найти релевантных сотрудников по LinkedIn Company → People
def find_company_people(
    linkedin_company_url: str,
    brand: Optional[str] = None,
) -> List[Dict[str, str]]:
    cfg = get_linkedin_cfg()
    if not cfg.get("enabled", False):
        logger.info("linkedin disabled in settings")
        return []

    people_url = _normalize_people_url(linkedin_company_url)
    if not people_url:
        logger.info("linkedin company url is empty")
        return []

    account = cfg.get("account") or {}
    cookies_raw = _decode_cookies_base64(account.get("cookies_base64", ""))
    cookies = _normalize_cookies_for_playwright(cookies_raw)
    ua = (account.get("useragent") or "").strip()

    # переведем в headful после минимальной правки JS; в этой версии только рендерим HTML)
    args = _build_playwright_args(people_url, ua, cookies, cfg)

    # TODO
    # proxy = cfg.get("proxy") or {}
    data = _run_playwright(args)
    if not data.get("ok"):
        logger.error("playwright result not ok for %s", people_url)
        return []

    # антибот/403: фиксируем, но не падаем
    anti = data.get("antiBot") or {}
    if anti.get("detected"):
        logger.warning(
            "anti-bot detected=%s kind=%s", anti.get("detected"), anti.get("kind")
        )

    html = data.get("html") or ""
    if not html:
        logger.info("no html body returned for %s", people_url)
        return []

    # парс HTML на сотрудников
    limit = int(cfg.get("max_profiles") or 25)
    role_filters = list(cfg.get("role_filters") or [])
    ppl = _parse_people_from_html(html, role_filters, limit)

    # отладочные логи
    logger.info(
        "linkedin people parsed: %s profiles (url=%s)",
        len(ppl),
        data.get("finalUrl") or people_url,
    )

    return ppl


# Служебный пошаговый помощник: прямой вызов для одной компании (для CLI-отладки)
def fetch_people_once(linkedin_company_url: str) -> None:
    people = find_company_people(linkedin_company_url)
    print(json.dumps(people, indent=2, ensure_ascii=False))


# CLI
if __name__ == "__main__":
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else ""
    if not url:
        print("Usage: python -m core.parser.linkedin <linkedin_company_url>")
        raise SystemExit(2)
    fetch_people_once(url)
