from __future__ import annotations

import logging

from app.adapters.crm.kommo import KommoAdapter
from domain.services.company_x import set_company_x
from fastapi import APIRouter, HTTPException, Query
from worker.tasks import seed_next_company

log = logging.getLogger("app.admin")
router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/kommo/add-note")
def add_note(lead_id: int = Query(...), text: str = Query(...)):
    KommoAdapter().add_note(lead_id, text)
    return {"ok": True, "lead_id": lead_id, "text": text}


@router.post("/company/{company_id}/set-x")
def set_company_x_route(
    company_id: int, x: str = Query(..., description="https://x.com/...")
):
    try:
        return set_company_x(company_id, x)
    except Exception as e:
        log.exception("set_company_x failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/seed/next")
def seed_next():
    async_result = seed_next_company.delay()
    return {"ok": True, "queued": True, "task_id": async_result.id}
