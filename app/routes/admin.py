from __future__ import annotations

import logging

from app.adapters.crm.kommo import KommoAdapter
from fastapi import APIRouter, HTTPException, Query
from worker.tasks import seed_next_company

log = logging.getLogger("app.admin")
router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/kommo/add-note")
def add_note(lead_id: int = Query(...), text: str = Query(...)):
    try:
        KommoAdapter().add_note(lead_id, text)
        return {"ok": True, "lead_id": lead_id, "text": text}
    except Exception as e:
        log.exception("add_note failed")
        raise HTTPException(status_code=500, detail=str(e))


# Следующий сайт из config/sites.yml,
@router.post("/seed/next")
def seed_next():
    async_result = seed_next_company.delay()
    return {"ok": True, "queued": True, "task_id": async_result.id}
