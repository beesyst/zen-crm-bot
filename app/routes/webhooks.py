from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Sequence, Union

from fastapi import APIRouter, HTTPException, Request, Response, status
from worker.tasks import enrich_company, kickoff_outreach

log = logging.getLogger("app.api")
router = APIRouter(prefix="/webhooks/kommo", tags=["kommo-webhooks"])


# Небольшой helper: безопасно проход по ключам и возврат первого найденного значения
def _get_first(path: Sequence[Union[str, int]], obj: Dict[str, Any]) -> Optional[Any]:
    cur: Any = obj
    try:
        for key in path:
            if isinstance(key, int):
                cur = cur[key]
            else:
                cur = cur.get(key)
            if cur is None:
                return None
        return cur
    except Exception:
        return None


# Kommo для companies
def _extract_company_id_from_form(form) -> Optional[int]:
    cand = (
        form.get("companies[add][0][id]")
        or form.get("contacts[add][0][id]")
        or form.get("entity[add][0][id]")
    )
    try:
        return int(cand) if cand else None
    except Exception:
        return None


# Kommo для лидов
def _extract_lead_id_from_form(form) -> Optional[int]:
    cand = form.get("leads[add][0][id]") or form.get("entity[add][0][id]")
    try:
        return int(cand) if cand else None
    except Exception:
        return None


# company.added - событие создания компании (Lists)
@router.post("/company.added")
async def company_added(req: Request):
    ct = (req.headers.get("content-type") or "").lower()

    company_id: Optional[int] = None
    if "application/json" in ct:
        data = await req.json()
        company_id = (
            _get_first(["companies", "add", 0, "id"], data)
            or _get_first(["contacts", "add", 0, "id"], data)
            or _get_first(["entity", "add", 0, "id"], data)
        )
    else:
        form = await req.form()
        company_id = _extract_company_id_from_form(form)

    if company_id:
        enrich_company.apply_async(args=[int(company_id)])
        # Kommo важно быстро получить 2xx - отдаем сразу
        return {"ok": True, "queued": True, "company_id": int(company_id)}

    # отдаем 200 с описанием (Kommo считает 2xx принятым сигналом)
    return Response(
        content=json.dumps({"ok": False, "reason": "no company_id"}),
        media_type="application/json",
        status_code=200,
    )


@router.post("/company.edited")
async def company_edited(req: Request):
    ct = (req.headers.get("content-type") or "").lower()

    company_id: Optional[int] = None
    if "application/json" in ct:
        data = await req.json()
        company_id = (
            _get_first(["companies", "update", 0, "id"], data)
            or _get_first(["contacts", "update", 0, "id"], data)
            or _get_first(["entity", "update", 0, "id"], data)
        )
    else:
        form = await req.form()
        company_id = (
            form.get("companies[update][0][id]")
            or form.get("contacts[update][0][id]")
            or form.get("entity[update][0][id]")
        )
        try:
            company_id = int(company_id) if company_id else None
        except Exception:
            company_id = None

    if company_id:
        enrich_company.apply_async(args=[int(company_id)])
        return {"ok": True, "queued": True, "company_id": int(company_id)}

    return Response(
        content=json.dumps({"ok": False, "reason": "no company_id"}),
        media_type="application/json",
        status_code=200,
    )


# lead.added - событие создания лида
@router.post("/lead.added", status_code=status.HTTP_202_ACCEPTED)
async def lead_added(req: Request):
    ct = (req.headers.get("content-type") or "").lower()

    lead_id: Optional[int] = None
    if "application/json" in ct:
        data = await req.json()
        lead_id = (
            _get_first(["leads", "add", 0, "id"], data)
            or data.get("lead_id")
            or data.get("id")
            or _get_first(["entity", "add", 0, "id"], data)
        )
    else:
        form = await req.form()
        lead_id = _extract_lead_id_from_form(form)

    if not lead_id:
        return {"ok": False, "reason": "no lead_id"}

    return {"ok": True, "queued": True, "lead_id": int(lead_id)}


# lead.updated - любое обновление лида (например, смена стадии/поля)
@router.post("/lead.updated", status_code=status.HTTP_202_ACCEPTED)
async def kommo_lead_updated(req: Request):
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # минимальная нормализация идентификатора
    lead_id = (
        payload.get("lead_id")
        or payload.get("id")
        or _get_first(["leads", "update", 0, "id"], payload)
        or _get_first(["entity", "update", 0, "id"], payload)
    )

    if not lead_id and not payload.get("fields"):
        raise HTTPException(status_code=400, detail="Missing lead_id or fields")

    log.info(
        "webhook.lead.updated",
        extra={"lead_id": lead_id, "keys": list(payload.keys())},
    )
    kickoff_outreach.delay(payload)
    return {"queued": True, "lead_id": lead_id}
