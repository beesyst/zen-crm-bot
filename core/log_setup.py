from __future__ import annotations

import logging
import os
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Optional

from core.paths import LOG_PATHS, LOGS_DIR, ensure_dirs
from core.settings import get_flag

# Контекст корреляции
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
lead_id_var: ContextVar[str | None] = ContextVar("lead_id", default=None)
task_id_var: ContextVar[str | None] = ContextVar("task_id", default=None)


# Полная очистка всех лог-файлов
def clear_all_logs():
    for name, path in LOG_PATHS.items():
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [INFO] - [log_setup] Лог очищен\n"
                )
        except Exception as e:
            print(f"[log_setup] Не удалось очистить {path}: {e}")


# Чтение config/settings.yml
def auto_clear_logs_if_needed():
    if not get_flag("clear_logs", False):
        return
    ensure_dirs()
    os.makedirs(LOGS_DIR, exist_ok=True)
    clear_all_logs()


def get_request_id() -> str:
    rid = request_id_var.get()
    if not rid:
        rid = uuid.uuid4().hex[:12]
        request_id_var.set(rid)
    return rid


def bind_lead_id(lead_id: str | int | None):
    if lead_id is not None:
        lead_id_var.set(str(lead_id))


def bind_task_id(task_id: str | None):
    if task_id:
        task_id_var.set(task_id)


# Фильтр с метками
class ContextFilter(logging.Filter):
    def __init__(self, service: str = "zen-crm", env: str = "dev"):
        super().__init__()
        self.service = service
        self.env = env

    def filter(self, record: logging.LogRecord) -> bool:
        # гарантируем наличие полей, если формат попросит
        record.service = getattr(record, "service", self.service)
        record.env = getattr(record, "env", self.env)
        record.request_id = request_id_var.get()
        record.lead_id = lead_id_var.get()
        record.task_id = task_id_var.get()
        return True


# Подгон под формат
def _make_formatter() -> logging.Formatter:
    # пример: 2025-08-20 17:43:08 [INFO] - [twitter_parser] msg (rid=..., lead=..., task=...)
    fmt = "%(asctime)s [%(levelname)s] - [%(name)s] %(message)s" "%(request_suffix)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    class _Formatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            # безопасно читаем атрибуты, даже если фильтр не отработал
            rid = getattr(record, "request_id", None)
            lid = getattr(record, "lead_id", None)
            tid = getattr(record, "task_id", None)

            parts = []
            if rid:
                parts.append(f"rid={rid}")
            if lid:
                parts.append(f"lead={lid}")
            if tid:
                parts.append(f"task={tid}")

            record.request_suffix = f" ({', '.join(parts)})" if parts else ""
            return super().format(record)

    return _Formatter(fmt=fmt, datefmt=datefmt)


def setup_logging(
    level: str = "INFO",
    service: str = "zen-crm",
    env: str = "dev",
    write_files: bool = True,
    split_files: bool = True,
    all_in_one_file: bool = True,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
):
    ensure_dirs()
    root = logging.getLogger()
    root.setLevel(level.upper())

    # чистим дефолтные хендлеры (uvicorn добавляет свои)
    for h in list(root.handlers):
        root.removeHandler(h)

    ctx_filter = ContextFilter(service=service, env=env)
    human_fmt = _make_formatter()

    # stdout (Docker будет это собирать)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(human_fmt)
    stream.addFilter(ctx_filter)
    stream.setLevel(root.level)
    root.addHandler(stream)

    if write_files:
        path = LOG_PATHS.get("host")
        if all_in_one_file and path:
            fh = RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
            fh.setFormatter(human_fmt)
            fh.addFilter(ctx_filter)
            fh.setLevel(root.level)

            for name in ("host", "orchestrator"):
                lg = logging.getLogger(name)
                lg.setLevel(root.level)
                lg.propagate = False
                already = any(
                    isinstance(h, RotatingFileHandler)
                    and getattr(h, "baseFilename", None) == fh.baseFilename
                    for h in lg.handlers
                )
                if not already:
                    lg.addHandler(fh)

        # отдельные файлы по именам логгеров (фракциям)
        if split_files:

            def attach_file(logger_name: str, key: str):
                path = LOG_PATHS.get(key)
                if not path:
                    return
                h = RotatingFileHandler(
                    path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
                )
                h.setFormatter(human_fmt)
                h.addFilter(ctx_filter)
                h.setLevel(root.level)
                logging.getLogger(logger_name).addHandler(h)

            attach_file("crm.kommo", "kommo")

    # успокаиваем шумные логгеры
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# Возврат именованного логгера
def get_logger(name: str, level: Optional[str] = None) -> logging.Logger:
    ensure_dirs()
    root = logging.getLogger()
    if not root.handlers:
        setup_logging()

    formatter = root.handlers[0].formatter
    logger = logging.getLogger(name)
    logger.setLevel(level.upper() if level else root.level)
    logger.propagate = False

    # локальный фильтр на каждый создаваемый хендлер
    local_filter = ContextFilter()

    need_path = LOG_PATHS.get(name)
    has_file = any(
        isinstance(h, (RotatingFileHandler, logging.FileHandler))
        for h in logger.handlers
    )
    if need_path and not has_file:
        fh = RotatingFileHandler(
            need_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(formatter)
        fh.addFilter(local_filter)
        fh.setLevel(logger.level)
        logger.addHandler(fh)

    has_any = len(logger.handlers) > 0
    if not has_any:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(formatter)
        sh.addFilter(local_filter)
        sh.setLevel(logger.level)
        logger.addHandler(sh)

    return logger
