from __future__ import annotations

from jinja2 import Template


def render_template(path, ctx: dict) -> str:
    with open(path, "r", encoding="utf-8") as f:
        tpl = Template(f.read())
    return tpl.render(**ctx)
