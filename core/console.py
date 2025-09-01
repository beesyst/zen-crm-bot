from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, Iterable, Tuple

# Спиннер
_SPINNER_ENABLED = os.getenv("CONSOLE_SPINNER", "1") not in ("0", "false", "False")

# Кадры и тайминги для "короткой" заставки (перед статусами ok/skip/... в пайплайнах)
_SPIN_FRAMES_SHORT: Iterable[str] = ("|", "/", "-", "\\")
_SPIN_DELAY_SHORT = 0.08
_SPIN_TICKS_SHORT = 6

# Кадры и тайминги для "длинного" спиннера (пока выполняется worker в step())
_SPIN_FRAMES_LONG: Iterable[str] = ("|", "/", "-", "\\")
_SPIN_DELAY_LONG = 0.12

# Выравнивание префиксов для ровных колонок
_PAD_OK = "[ok] "
_PAD_ADD = "[add] "
_PAD_UPD = "[update] "
_PAD_SKIP = "[skip] "
_PAD_ERR = "[error] "


# Базовая точка вывода в stdout с переводом строки и flush
def _emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


# Точный вывод без перевода строки (используется спиннером)
def _emit_inline(line: str) -> None:
    sys.stdout.write(line)
    sys.stdout.flush()


# Очистка текущей строки в терминале
def _clear_inline() -> None:
    _emit_inline("\r")


# Короткая заставка-анимация для статусов ok/add/update/skip
def _spin_once_short(msg: str) -> None:
    if not _SPINNER_ENABLED:
        return
    _emit(f"[/] {msg}")
    time.sleep(_SPIN_DELAY_SHORT)


# Рамка начала пайплайна, спиннер не нужен
def start() -> None:
    _emit("Start")


# Рамка завершения пайплайна, спиннер не нужен.
def finish() -> None:
    _emit("Finish")


# Позитивный короткий статус (например, 'start enrich', 'total: 3')
def ok(msg: str) -> None:
    _spin_once_short(msg)
    _emit_inline("\r")
    _emit(f"{_PAD_OK}{msg}")


# Статус add с временем выполнения
def add(url: str, s: int) -> None:
    msg = f"{url} - {s} sec"
    _spin_once_short(msg)
    _clear_inline()
    _emit(f"{_PAD_ADD}{msg}")


# Статус update с временем выполнения
def update(url: str, s: int) -> None:
    msg = f"{url} - {s} sec"
    _spin_once_short(msg)
    _clear_inline()
    _emit(f"{_PAD_UPD}{msg}")


# Статус skip с опциональной причиной
def skip(url: str, why: str = "") -> None:
    suffix = f" ({why})" if why else ""
    msg = f"{url}{suffix}"
    _spin_once_short(msg)
    _clear_inline()
    _emit(f"{_PAD_SKIP}{msg}")


# Ошибка: печать сразу без спиннера
def error(url: str, err: str) -> None:
    msg = f"{url} - {err}"
    _spin_once_short(msg)
    _clear_inline()
    _emit(f"{_PAD_ERR}{msg}")


# Универсальный длинный спиннер для setup-шагов
def step(label: str, worker: Callable[[], Tuple[bool, str | None]]) -> bool:
    done = threading.Event()
    state = {"ok": False, "suffix": ""}

    def spinner():
        if not _SPINNER_ENABLED:
            return
        frames = list(_SPIN_FRAMES_LONG)
        i = 0
        while not done.is_set():
            frame = frames[i % len(frames)]
            _emit_inline(f"\r[{frame}] {label}")
            time.sleep(_SPIN_DELAY_LONG)
            i += 1
        _clear_inline()

    t = threading.Thread(target=spinner, daemon=True)
    t.start()
    try:
        ok_, suffix = worker()
        state["ok"] = bool(ok_)
        state["suffix"] = (suffix or "").strip()
    except Exception:
        state["ok"] = False
        state["suffix"] = ""
    finally:
        done.set()
        t.join()

    prefix = "[ok]" if state["ok"] else "[error]"
    tail = f" {state['suffix']}" if state["suffix"] else ""
    _emit(f"{prefix} {label}{tail}")
    return state["ok"]
