from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional, Tuple

import yaml

STATE_PATH = "/app/storage/seed/state.json"


def _ensure_state():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    if not os.path.exists(STATE_PATH):
        with open(STATE_PATH, "w") as f:
            json.dump({"next_idx": 0}, f)


def _load_state() -> Dict[str, Any]:
    _ensure_state()
    with open(STATE_PATH, "r") as f:
        return json.load(f)


def _save_state(st: Dict[str, Any]) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(st, f)


def _load_sites() -> list[str]:
    path = "config/sites.yml"
    with open(path, "r") as f:
        data = yaml.safe_load(f) or {}
    return data.get("sites") or []


def _name_from_url(url: str) -> str:
    # https://foo.io -> Foo
    host = re.sub(r"^https?://", "", url).split("/")[0]
    host = re.sub(r"^www\.", "", host)
    core = host.split(".")[0]
    return core.capitalize() if core else "Project"


def get_next_site() -> Tuple[Optional[Dict[str, str]], int, int]:
    sites = _load_sites()
    total = len(sites)
    st = _load_state()
    i = int(st.get("next_idx", 0))
    if i >= total:
        return None, i, total
    url = sites[i]
    return {"website": url, "name": _name_from_url(url)}, i, total


def mark_done():
    st = _load_state()
    st["next_idx"] = int(st.get("next_idx", 0)) + 1
    _save_state(st)
