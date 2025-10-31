from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from core.paths import PROJECT_ROOT

# Базовая директория для данных
DATA_DIR = PROJECT_ROOT / "storage"
NEWS_DIR = DATA_DIR / "news"
LATEST_FILENAME = "latest.json"

DEFAULT_LATEST_LIMIT = 500


# Результат сохранения элемента
@dataclass
class SaveResult:
    path: Path
    is_new: bool
    is_updated: bool


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _json_load(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _json_dump(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _news_day_dir(project_key: str, d: Optional[date]) -> Path:
    d = d or datetime.utcnow().date()
    return NEWS_DIR / project_key / d.strftime("%Y-%m-%d")


def _normalize_id(uid: str) -> str:
    # безопасное имя файла (если вдруг в id мусор)
    return (
        "".join(ch for ch in uid.strip() if ch.isalnum() or ch in ("-", "_"))[:200]
        or "item"
    )


def _index_path(project_key: str) -> Path:
    return NEWS_DIR / project_key / LATEST_FILENAME


def _compare_shallow(a: dict, b: dict) -> bool:
    """Грубая проверка равенства (без порядка ключей)."""
    try:
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    except Exception:
        return False


def save_news_item(
    project_key: str,
    uid: str,
    item: dict,
    *,
    when: Optional[datetime] = None,
    latest_limit: int = DEFAULT_LATEST_LIMIT,
) -> SaveResult:
    """
    Универсальное сохранение новости:
      - файл: storage/news/<project>/<YYYY-MM-DD>/<id>.json
      - индекс: storage/news/<project>/latest.json (массив dict'ов, без дублей по id)
    Возвращает флаги: создано/обновлено.
    """
    # 1) каталоги
    _ensure_dir(NEWS_DIR / project_key)

    # 2) дата
    ts = when or _parse_ts(item.get("ts"))
    day_dir = _news_day_dir(project_key, (ts or datetime.utcnow()).date())
    _ensure_dir(day_dir)

    # 3) путь файла и старая версия
    fname = _normalize_id(uid) + ".json"
    fpath = day_dir / fname
    old = _json_load(fpath) if fpath.exists() else None

    # 4) обогащаем минимальными полями
    to_save = dict(item)
    to_save.pop("project_key", None)
    to_save.setdefault("project", project_key)
    to_save.setdefault("id", uid)
    if ts:
        # нормализуем ts к ISO + Z
        if isinstance(to_save.get("ts"), str) and to_save["ts"].endswith("Z"):
            pass
        else:
            to_save["ts"] = ts.isoformat() + "Z"

    # 5) решаем: новый/обновлённый/без изменений
    is_new = old is None
    is_updated = False
    if old is None:
        _json_dump(fpath, to_save)
        is_updated = False
    else:
        if not _compare_shallow(old, to_save):
            _json_dump(fpath, to_save)
            is_updated = True

    # 6) поддерживаем latest.json (без дублей по id)
    _update_latest_index(project_key, to_save, latest_limit=latest_limit)

    return SaveResult(path=fpath, is_new=is_new, is_updated=is_updated)


def _update_latest_index(project_key: str, item: dict, *, latest_limit: int) -> None:
    ipath = _index_path(project_key)
    index = _json_load(ipath) or []

    # дедуп по id
    iid = str(item.get("id") or "")
    seen = set()
    out = []
    placed = False

    # вставляем/обновляем первым элементом
    if iid:
        out.append(item)
        seen.add(iid)
        placed = True

    # прокатываем старые
    for it in index:
        oid = str((it or {}).get("id") or "")
        if not oid or oid in seen:
            continue
        out.append(it)
        seen.add(oid)

    # обрезаем
    if latest_limit > 0 and len(out) > latest_limit:
        out = out[:latest_limit]

    _ensure_dir(ipath.parent)
    _json_dump(ipath, out)


def _parse_ts(v: Any) -> Optional[datetime]:
    if isinstance(v, (int, float)):
        try:
            return datetime.utcfromtimestamp(v)
        except Exception:
            return None
    if isinstance(v, str) and v:
        s = v[:-1] if v.endswith("Z") else v
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None
    return None
