from __future__ import annotations

import sys

# Флаг, разрешающий дублирование в stdout (по умолчанию - выключено)
_CONSOLE_STDOUT = True


# Базовая точка вывода
def _emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


# Формирование строки вида
def _fmt(tag: str, msg: str) -> str:
    return f"{tag}{msg}" if msg else f"{tag}".rstrip()


# Метка начала пайплайна
def start() -> None:
    _emit(_fmt("Start", ""))


# Метка завершения пайплайна
def finish() -> None:
    _emit(_fmt("Finish", ""))


# Метка успешной операции/статуса
def ok(msg: str) -> None:
    _emit(_fmt("[ok]    ", msg))


# Метка 'добавлено' с временем выполнения
def add(url: str, s: int) -> None:
    _emit(_fmt("[add]   ", f"{url} - {s} sec"))


# Метка 'обновлено' с временем выполнения
def update(url: str, s: int) -> None:
    _emit(_fmt("[update]", f"{url} - {s} sec"))


# Метка 'пропущено' с причиной (опционально)
def skip(url: str, why: str = "") -> None:
    suffix = f" ({why})" if why else ""
    _emit(_fmt("[skip]  ", f"{url}{suffix}"))


# Метка ошибки с текстом исключения/ошибки
def error(url: str, err: str) -> None:
    _emit(_fmt("[error] ", f"{url} - {err}"))
