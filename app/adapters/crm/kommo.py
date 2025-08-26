from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import requests
from core.settings import get_settings

log = logging.getLogger("crm.kommo")


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
        url = f"{self.base}{path}"
        resp = requests.request(method, url, headers=self.headers, timeout=30, **kw)
        if resp.status_code >= 400:
            # не падаем без логов: поможет диагностировать неправильные id полей/стадий
            body = resp.text[:500].replace("\n", " ")
            log.error("kommo %s %s -> %s %s", method, path, resp.status_code, body)
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
