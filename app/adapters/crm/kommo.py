from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import requests
from core.log_setup import get_logger
from core.settings import get_settings

log = get_logger("kommo")


def _conf():
    cfg = get_settings()
    k = cfg["crm"]["kommo"]
    base = str(k["base_url"]).rstrip("/")
    token = str(k["access_token"])
    secret = str(
        k.get("secret_key", "")
    )  # опционально — пригодится для подписи вебхуков
    stages_path = Path(k.get("stages_map", "config/stages.map.json"))
    fields = k.get("fields", {}) or {}
    if not base or not token:
        raise RuntimeError("config: crm.kommo.base_url / access_token are required")
    return base, token, secret, stages_path, fields


def _load_stage(stages_path: Path, code: str) -> Dict[str, int]:
    if not stages_path.exists():
        raise RuntimeError(f"Stages map not found: {stages_path}")
    data = json.loads(stages_path.read_text(encoding="utf-8") or "{}")
    node = data.get(code)
    if not node or "pipeline_id" not in node or "status_id" not in node:
        raise RuntimeError(f"Stage code '{code}' not found in {stages_path}")
    return {
        "pipeline_id": int(node["pipeline_id"]),
        "status_id": int(node["status_id"]),
    }


# Тонкий клиент Kommo v4: только HTTP-вызовы и формирование payload
class KommoAdapter:
    def __init__(self) -> None:
        self.base, self.token, self.secret, self.stages_path, self.fields = _conf()
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # http
    def _req(self, method: str, path: str, **kw) -> Any:
        url = path if str(path).startswith("http") else f"{self.base}{path}"
        params = kw.get("params")
        # пишем и URL, и params
        log.info("HTTP %s %s params=%s", method, url, params)
        resp = requests.request(method, url, headers=self.headers, timeout=30, **kw)
        log.info("HTTP %s %s -> %s", method, url, resp.status_code)
        if resp.status_code >= 400:
            body = resp.text[:500].replace("\n", " ")
            log.error("kommo %s %s -> %s %s", method, url, resp.status_code, body)
            raise RuntimeError(f"Kommo API error {resp.status_code}")
        return resp.json() if resp.text.strip() else {}

    # public api
    def get_lead(self, lead_id: int) -> Dict[str, Any]:
        return self._req("GET", f"/api/v4/leads/{lead_id}")

    def add_note(self, lead_id: int, text: str) -> None:
        payload = [{"note_type": "common", "params": {"text": text}}]
        self._req("POST", f"/api/v4/leads/{lead_id}/notes", json=payload)

    def set_stage(self, lead_id: int, stage_code: str) -> None:
        ids = _load_stage(self.stages_path, stage_code)
        body = {"pipeline_id": ids["pipeline_id"], "status_id": ids["status_id"]}
        self._req("PATCH", f"/api/v4/leads/{lead_id}", json=body)

    def update_lead(self, lead_id: int, patch: Dict[str, Any]) -> Dict[str, Any]:
        return self._req("PATCH", f"/api/v4/leads/{lead_id}", json=patch)

    def update_custom_fields(self, lead_id: int, kv: Dict[int, Any]) -> None:
        if not kv:
            return
        cf = []
        for fid, val in kv.items():
            if val is None or val == "":
                continue
            cf.append({"field_id": int(fid), "values": [{"value": val}]})
        if not cf:
            return
        body = {"custom_fields_values": cf}
        self._req("PATCH", f"/api/v4/leads/{lead_id}", json=body)

    def add_tags(self, lead_id: int, tags: List[str]) -> None:
        tags = [t for t in (tags or []) if t]
        if not tags:
            return
        body = {"_embedded": {"tags": [{"name": t} for t in tags]}}
        self._req("PATCH", f"/api/v4/leads/{lead_id}", json=body)

    def create_company(
        self,
        name: str,
        website: str | None = None,
        tags: list[str] | None = None,
        custom_fields: dict[int, Any] | None = None,
    ) -> int:
        cf: list[dict[str, Any]] = []

        # пишем сайт
        web_fid = None
        try:
            if self.fields.get("web"):
                web_fid = int(self.fields["web"])
            elif self.fields.get("site"):
                web_fid = int(self.fields["site"])
        except Exception:
            web_fid = None

        if web_fid and website:
            cf.append({"field_id": web_fid, "values": [{"value": website}]})

        if custom_fields:
            for fid, val in custom_fields.items():
                if val not in (None, ""):
                    cf.append({"field_id": int(fid), "values": [{"value": val}]})

        body: dict[str, Any] = {"name": name}
        if cf:
            body["custom_fields_values"] = cf
        if tags:
            body["_embedded"] = {"tags": [{"name": t} for t in tags if t]}

        res = self._req("POST", "/api/v4/companies", json=[body])
        companies = (res or {}).get("_embedded", {}).get("companies") or []
        if not companies:
            raise RuntimeError("Kommo: empty create company response")
        return int(companies[0]["id"])

    def update_company_custom_fields(self, company_id: int, kv: dict[int, Any]) -> None:
        log.info("PATCH company %s custom_fields: %s", company_id, list(kv.keys()))
        if not kv:
            return
        cf = []
        for fid, val in kv.items():
            if val not in (None, ""):
                cf.append({"field_id": int(fid), "values": [{"value": val}]})
        if not cf:
            return
        body = {"custom_fields_values": cf}
        self._req("PATCH", f"/api/v4/companies/{company_id}", json=body)

    def add_company_tags(self, company_id: int, tags: list[str]) -> None:
        tags = [t for t in (tags or []) if t]
        if not tags:
            return
        body = {"_embedded": {"tags": [{"name": t} for t in tags]}}
        self._req("PATCH", f"/api/v4/companies/{company_id}", json=body)

    def add_company_note(self, company_id: int, text: str) -> None:
        payload = [{"note_type": "common", "params": {"text": text}}]
        self._req("POST", f"/api/v4/companies/{company_id}/notes", json=payload)

    def get_company(self, company_id: int) -> Dict[str, Any]:
        return self._req("GET", f"/api/v4/companies/{company_id}")

    def get_company_tags(self, company: Dict[str, Any]) -> List[str]:
        return [
            t.get("name", "") for t in (company.get("_embedded", {}).get("tags") or [])
        ]

    # Резолв имен тегов (companies) в ID через /api/v4/tags
    def resolve_tag_ids(self, names: list[str]) -> list[int]:
        out: list[int] = []
        for name in names or []:
            q = (name or "").strip()
            if not q:
                continue
            params = {"filter[entity_type]": "companies", "query": q, "limit": 50}
            url = "/api/v4/tags"
            # идем по страницам, пока не найдем точное совпадение
            while url:
                data = (
                    self._req(
                        "GET", url, params=params if str(url).startswith("/") else None
                    )
                    or {}
                )
                for t in (data.get("_embedded") or {}).get("tags") or []:
                    tname = (t.get("name") or "").strip()
                    if tname.lower() == q.lower() and t.get("id"):
                        out.append(int(t["id"]))
                        url = None
                        break
                if url:
                    next_link = (data.get("_links") or {}).get("next", {}).get("href")
                    url, params = (next_link, None) if next_link else (None, None)
        return out

    # сайт компании из custom_fields / верхнего уровня
    def get_company_web(self, company: Dict[str, Any]) -> str:
        fields = (
            company.get("custom_fields_values") or company.get("custom_fields") or []
        )
        # приоритет - явные поля web/site из настроек
        web_fids = []
        try:
            if self.fields.get("web"):
                web_fids.append(int(self.fields["web"]))
            if self.fields.get("site"):
                web_fids.append(int(self.fields["site"]))
        except Exception:
            pass

        # сначала ищем по id
        for f in fields:
            fid = f.get("field_id")
            if fid in web_fids:
                for v in f.get("values") or []:
                    val = (v.get("value") or "").strip()
                    if val.startswith("http"):
                        return val

        # затем по названию/коду
        for f in fields:
            code = (f.get("code") or "").lower()
            name = (f.get("name") or "").lower()
            if code in ("website", "web", "url") or "site" in name or "web" in name:
                for v in f.get("values") or []:
                    val = (v.get("value") or "").strip()
                    if val.startswith("http"):
                        return val

        # фолбэк - поле верхнего уровня
        return (company.get("website") or "").strip()

    # Серверная фильтрация
    def iter_companies_by_tag_ids(
        self,
        tag_ids: list[int],
        limit: int = 250,
    ):
        if not tag_ids:
            return
        url = "/api/v4/companies"
        lim = max(1, min(int(limit or 250), 250))
        params = [
            ("limit", str(lim)),
            ("with", "custom_fields,contacts,tags"),
            ("filter[tags_logic]", "or"),
            ("useFilter", "y"),
        ]
        # как в UI: filter[tags][]=157965 (повторяем ключ для каждого id)
        for tid in tag_ids:
            params.append(("filter[tags][]", str(tid)))

        while url:
            data = (
                self._req(
                    "GET", url, params=params if str(url).startswith("/") else None
                )
                or {}
            )
            items = (data.get("_embedded") or {}).get("companies") or []

            # клиентская проверка на всякий случай (без доп. запросов)
            required = set(int(x) for x in (tag_ids or []))
            for it in items:
                tags = ((it.get("_embedded") or {}).get("tags")) or []
                have = {int(t["id"]) for t in tags if t.get("id")}
                if have & required:
                    yield it

            next_link = (data.get("_links") or {}).get("next", {}).get("href")
            url, params = (next_link, None) if next_link else (None, None)
