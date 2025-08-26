from __future__ import annotations

import importlib
from typing import Any, Dict, List

from core.settings import get_settings
from modules.base import OutreachChannel

_state: Dict[str, Any] = {"outreach": []}


def _instantiate(dotted: str):
    # dotted может быть "pkg.mod:Class" или "pkg.mod.Class"
    if ":" in dotted:
        mod, cls = dotted.split(":", 1)
    else:
        mod, cls = dotted.rsplit(".", 1)
    m = importlib.import_module(mod)
    C = getattr(m, cls)
    return C()


def init_modules():
    cfg = get_settings()
    _state["outreach"] = []
    for ch_path in cfg["modules"].get("outreach", []):
        _state["outreach"].append(_instantiate(ch_path))


def get_outreach_channels() -> List[OutreachChannel]:
    if not _state["outreach"]:
        init_modules()
    return _state["outreach"]
