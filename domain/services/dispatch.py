from __future__ import annotations

from typing import Dict

from core.paths import TEMPLATES_DIR
from infra.templating.jinja import render_template


# Подготовка subject/body для e-mail на основе шаблона
def build_email(ctx: Dict) -> Dict:
    project_name = ctx.get("project_name") or "Project"
    template_path = TEMPLATES_DIR / "email_outreach.html"
    html = render_template(
        template_path,
        {
            "project_name": project_name,
            "name": "",
            "value_prop": "Коротко о ценности: …",
            "cal_link": "#",
            "sender_name": "Noders",
            "company": "Noders",
        },
    )
    ctx["email_subject"] = f"Партнёрство по {project_name}"
    ctx["email_html"] = html
    return ctx
