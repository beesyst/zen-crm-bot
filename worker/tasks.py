from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from celery import Celery, chain, chord, group
from core.log_setup import bind_lead_id, bind_task_id, setup_logging
from core.paths import ensure_dirs
from core.settings import get_settings

# Домен
from domain.services.dedupe import dedupe_contacts
from domain.services.ingest import ingest_from_website
from domain.services.intake import bootstrap_new_lead as _bootstrap_new_lead
from domain.services.plan import build_outreach_plan
from domain.services.seed import get_next_site, mark_done

# Реестр каналов
from modules.registry import get_outreach_channels

# Опциональная интеграция для companies
_enrich_company: Optional[callable] = None
try:
    from domain.services.companies import enrich_company as _enrich_company
except Exception:
    _enrich_company = None

# Папки/логи
ensure_dirs()
setup_logging(level="INFO", service="zen-crm", env="dev", write_files=True)
log = logging.getLogger("worker")

# Конфиг / Celery
cfg = get_settings()
REDIS_URL = cfg["infra"]["redis_url"]
celery = Celery(__name__, broker=REDIS_URL, backend=REDIS_URL)
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_time_limit=60 * 10,
    task_soft_time_limit=60 * 8,
)

celery.conf.beat_schedule = {
    "seed-next-company": {
        "task": "seed_next_company",
        "schedule": 60.0,
    }
}


# Хелперы
def _build_initial_ctx(payload: Dict[str, Any]) -> Dict[str, Any]:
    lead_id = payload.get("lead_id") or payload.get("id")
    fields = payload.get("fields") or {}

    website = fields.get("website") or payload.get("website") or ""
    project_name = (
        payload.get("project_name")
        or payload.get("name")
        or fields.get("name")
        or "Project"
    )

    raw_emails = fields.get("emails") or payload.get("emails") or []
    if isinstance(raw_emails, str):
        emails = [e.strip() for e in raw_emails.split(",") if e.strip()]
    else:
        emails = list(dict.fromkeys([str(e).strip() for e in (raw_emails or [])]))

    ctx: Dict[str, Any] = {
        "lead_id": lead_id,
        "project_name": project_name,
        "website": website.strip(),
        "emails": emails,
        "discord_webhook": fields.get("discord_webhook")
        or fields.get("discord_webhook_url"),
        "contact_form_urls": fields.get("contact_form_urls")
        or fields.get("contact_form_url"),
        "telegram_chat_id": fields.get("telegram_chat_id"),
        "raw_fields": fields,
    }
    return ctx


# Tasks
@celery.task(name="bootstrap_new_lead")
def bootstrap_new_lead(lead_id: int) -> dict:
    try:
        return _bootstrap_new_lead(lead_id)
    except Exception:
        log.exception("bootstrap_new_lead.error", extra={"lead_id": lead_id})
        return {"lead_id": lead_id, "error": True}


@celery.task(name="t_ingest")
def t_ingest(ctx: Dict[str, Any]) -> Dict[str, Any]:
    website = (ctx.get("website") or "").strip()
    if not website:
        log.info("ingest.skip", extra={"event": "ingest.skip"})
        return ctx
    try:
        found = ingest_from_website(
            website=website, project_name=ctx.get("project_name") or "Project"
        )
        if isinstance(found, dict):
            # emails
            ctx["emails"] = list(
                dict.fromkeys((ctx.get("emails") or []) + (found.get("emails") or []))
            )
            # формы
            forms = found.get("contact_form_urls") or found.get("forms") or []
            if isinstance(forms, str):
                forms = [u.strip() for u in forms.split(",") if u.strip()]
            if forms:
                ctx["contact_form_urls"] = forms
            # dc/tg
            ctx["discord_webhook"] = found.get("discord_webhook") or ctx.get(
                "discord_webhook"
            )
            ctx["telegram_chat_id"] = found.get("telegram_chat_id") or ctx.get(
                "telegram_chat_id"
            )
            # raw_fields
            rf = dict(ctx.get("raw_fields") or {})
            rf.update(found.get("raw_fields") or {})
            ctx["raw_fields"] = rf
        return ctx
    except Exception:
        log.exception(
            "ingest.error", extra={"event": "ingest.error", "website": website}
        )
        return ctx


@celery.task(name="t_dedupe")
def t_dedupe(ctx: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return dedupe_contacts(ctx) or ctx
    except Exception:
        log.exception(
            "dedupe.error",
            extra={"event": "dedupe.error", "lead_id": ctx.get("lead_id")},
        )
        return ctx


@celery.task(name="t_plan")
def t_plan(ctx: Dict[str, Any]) -> Dict[str, Any]:
    try:
        plan = build_outreach_plan(ctx) or {}
        if not plan.get("jobs"):
            log.info(
                "plan.empty",
                extra={"event": "plan.empty", "lead_id": ctx.get("lead_id")},
            )
        else:
            log.info(
                "plan.ready",
                extra={
                    "event": "plan.ready",
                    "lead_id": ctx.get("lead_id"),
                    "kinds": [j["kind"] for j in plan["jobs"]],
                },
            )
        # вернем plan дальше по цепочке
        return {"lead_id": ctx.get("lead_id"), **plan}
    except Exception:
        log.exception(
            "plan.error", extra={"event": "plan.error", "lead_id": ctx.get("lead_id")}
        )
        return {"lead_id": ctx.get("lead_id"), "jobs": []}


@celery.task(name="t_send")
def t_send(one_job: Dict[str, Any]) -> Dict[str, Any]:
    kind = one_job.get("kind")
    job = one_job.get("job") or {}
    try:
        for ch in get_outreach_channels():
            if ch.kind == kind:
                res = ch.send(job) or {}
                ok = bool(res.get("ok"))
                meta = res.get("meta") or {}
                return {"kind": kind, "ok": ok, "meta": meta}
        return {"kind": kind, "ok": False, "meta": {"error": "channel_not_found"}}
    except Exception as e:
        log.exception("send.error", extra={"event": "send.error", "kind": kind})
        return {"kind": kind, "ok": False, "meta": {"error": str(e)}}


@celery.task(name="t_finalize")
def t_finalize(lead_id: int, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        ok_kinds = [r["kind"] for r in results if r.get("ok")]
        fail_kinds = [r["kind"] for r in results if not r.get("ok")]

        log.info(
            "pipeline.finalize",
            extra={
                "event": "pipeline.finalize",
                "lead_id": lead_id,
                "ok_via": ok_kinds,
                "fail_via": fail_kinds,
            },
        )

        # пишем в Kommo, если доступен адаптер
        try:
            from app.adapters.crm.kommo import KommoAdapter

            crm = KommoAdapter()
            if ok_kinds:
                crm.add_note(lead_id, f"Outreach sent via: {', '.join(ok_kinds)}")
                crm.set_stage(lead_id, "OUTREACH_SENT")
            else:
                crm.add_note(lead_id, "Outreach plan had no executable channels.")
        except Exception:
            log.debug("crm.note.skip")

        return {
            "lead_id": lead_id,
            "ok": all(r.get("ok") for r in results),
            "via_ok": ok_kinds,
            "via_fail": fail_kinds,
        }
    except Exception:
        log.exception(
            "finalize.error", extra={"event": "finalize.error", "lead_id": lead_id}
        )
        return {"lead_id": lead_id, "ok": False, "via_ok": [], "via_fail": []}


@celery.task(name="t_dispatch_and_finalize")
def t_dispatch_and_finalize(plan: Dict[str, Any], lead_id: int) -> Dict[str, Any]:
    """
    Принимает plan (из t_plan) и синхронно запускает chord(group(send jobs), finalize).
    Возвращает мету с chord_id.
    """
    jobs = plan.get("jobs") or []
    if not jobs:
        log.info("dispatch.skip", extra={"event": "dispatch.skip", "lead_id": lead_id})
        # финализировать тут нечего - вернем пустой результат
        return {"lead_id": lead_id, "queued": False, "reason": "no_jobs"}

    header = group([t_send.s(j) for j in jobs])
    res = chord(header, t_finalize.s(lead_id)).apply_async()
    return {"lead_id": lead_id, "queued": True, "chord_id": res.id}


# entrypoint from webhook
@celery.task(bind=True, name="kickoff_outreach")
def kickoff_outreach(self, payload: Dict[str, Any]) -> Dict[str, Any]:
    bind_task_id(self.request.id)

    lead_id = payload.get("lead_id") or payload.get("id")
    if lead_id:
        bind_lead_id(lead_id)

    ctx = _build_initial_ctx(payload)

    log.info(
        "pipeline.start",
        extra={
            "event": "pipeline.start",
            "lead_id": lead_id,
            "website": ctx.get("website"),
        },
    )

    # цепочка: ingest -> dedupe -> plan -> dispatch+finalize
    sig = chain(
        t_ingest.s(ctx),
        t_dedupe.s(),
        t_plan.s(),
        t_dispatch_and_finalize.s(lead_id),
    )
    async_result = sig.apply_async()
    return {"queued": True, "lead_id": lead_id, "task_id": async_result.id}


@celery.task(name="enrich_company")
def enrich_company(company_id: int) -> dict:
    if _enrich_company is None:
        log.error("enrich_company service missing", extra={"company_id": company_id})
        return {"company_id": company_id, "ok": False, "reason": "service missing"}
    try:
        res = _enrich_company(int(company_id))
        return {"company_id": company_id, "ok": True, **(res or {})}
    except Exception:
        log.exception("enrich_company.error", extra={"company_id": company_id})
        return {"company_id": company_id, "ok": False, "error": True}


@celery.task(name="seed_next_company")
def seed_next_company() -> dict:
    try:
        item, idx, total = get_next_site()
        if not item:
            log.info("seed.done", extra={"event": "seed.done"})
            return {"ok": True, "done": True, "idx": idx, "total": total}

        from app.adapters.crm.kommo import KommoAdapter

        crm = KommoAdapter()

        # создаем компанию в Kommo
        company_id = crm.create_company(
            name=f"NEW {item['name']}", website=item["website"], tags=["NEW"]
        )

        # пробуем запустить обогащение
        try:
            enrich_company.delay(company_id)
        except Exception:
            log.debug("seed.enrich.skip")

        mark_done()
        log.info(
            "seed.created",
            extra={
                "company_id": company_id,
                "website": item["website"],
                "idx": idx,
                "total": total,
            },
        )
        return {
            "ok": True,
            "company_id": company_id,
            "website": item["website"],
            "idx": idx,
            "total": total,
        }
    except Exception:
        log.exception("seed.error")
        return {"ok": False, "error": True}
