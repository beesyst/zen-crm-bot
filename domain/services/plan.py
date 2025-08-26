# zen-crm-bot/domain/services/plan.py
from __future__ import annotations

from typing import Any, Dict, List

from core.settings import get_settings
from modules.registry import get_outreach_channels


def _safe_mode() -> bool:
    return bool(get_settings().get("app", {}).get("safe_mode", False))

# Сбор списка заданий для рассылки
def build_outreach_plan(ctx: Dict[str, Any]) -> Dict[str, Any]:
    jobs: List[Dict[str, Any]] = []
    for ch in get_outreach_channels():
        try:
            if hasattr(ch, "plan"):
                produced = ch.plan(ctx) or []
                for j in produced:
                    jobs.append({"kind": ch.kind, "job": j})
        except Exception:
            # не валим весь план, если один канал упал
            continue

    return {
        "lead_id": ctx.get("lead_id"),
        "jobs": jobs,
    }
