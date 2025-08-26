from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

class OutreachChannel(ABC):
    kind: str  # e.g. "email" | "discord" | "telegram" | "form"

    @abstractmethod
    def available(self, ctx: Dict[str, Any]) -> bool:
        """Есть ли всё необходимое для отправки по этому каналу?"""

    @abstractmethod
    def build_job(self, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Вернёт payload для send(); None, если нечего слать."""

    @abstractmethod
    def send(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """Собственно отправка. Возвращает {'ok': bool, 'meta': {...}}."""
