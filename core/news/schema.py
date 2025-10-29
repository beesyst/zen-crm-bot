from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.normalize import _strip_tracking_params, normalize_url
from pydantic import BaseModel, ConfigDict, Field, field_validator


# Ф-ция: сейчас в UTC - используется как дефолт для ts
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Модель новости с жесткой валидацией/нормализацией полей
class NewsItem(BaseModel):
    # конфиг: игнорировать лишние поля, обрезать пробелы в строках
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    # обязательные поля идентификации
    id: str
    source: str
    project_key: str

    # контент
    title: Optional[str] = None
    body: Optional[str] = None

    # ссылки/метаданные
    url: Optional[str] = None
    author: Optional[str] = None
    channel: Optional[str] = None
    thread_ts: Optional[str] = None

    # время: внутри модели - datetime в UTC; по запросу отдадим ISO-строку
    ts: datetime = Field(default_factory=_utcnow)

    # доп
    tags: List[str] = Field(default_factory=list)
    visibility: Optional[str] = None
    attachments: List[Dict[str, Any]] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)

    # валидатор: url → нормализованный, без трекинга; пустые превращаем в None
    @field_validator("url")
    @classmethod
    def _clean_url(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        u = normalize_url(v)
        if not u:
            return None
        return _strip_tracking_params(u)

    # валидатор: обязательные строковые поля не должны быть пустыми
    @field_validator("id", "source", "project_key")
    @classmethod
    def _required_non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("must be non-empty")
        return v

    # валидатор: привести список тегов к компактному виду без пустых значений
    @field_validator("tags")
    @classmethod
    def _compact_tags(cls, v: List[str]) -> List[str]:
        if not v:
            return []
        out = []
        seen = set()
        for t in v:
            s = (t or "").strip()
            if s and s not in seen:
                out.append(s)
                seen.add(s)
        return out


# Ф-ция: принять сырые dict-данные и вернуть нормализованный dict для JSON/хранилища
def shape_item(d: dict) -> dict:
    item = NewsItem.model_validate(d)
    return item.model_dump(mode="json")


# Ф-ция: только проверить, что запись валидна (исключение при ошибке)
def validate_item(d: dict) -> None:
    NewsItem.model_validate(d)
