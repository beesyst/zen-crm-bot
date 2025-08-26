from __future__ import annotations

import logging
from typing import Dict

log = logging.getLogger("domain.dedupe")


# Простейший дедуп: удаление повторяющихся emails
def dedupe_contacts(ctx: Dict) -> Dict:
    emails = ctx.get("emails", [])
    filtered = []
    seen = set()
    for e in emails:
        low = e.lower()
        if any(
            bad in low
            for bad in ["no-reply@", "noreply@", "do-not-reply", "donotreply"]
        ):
            continue
        if low not in seen:
            seen.add(low)
            filtered.append(e)
    ctx["emails"] = filtered
    log.info(
        "dedupe.summary",
        extra={"event": "dedupe.summary", "emails_after": len(filtered)},
    )
    return ctx
