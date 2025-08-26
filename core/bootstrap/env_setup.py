# zen-crm/core/bootstrap/env_setup.py
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# Централизованные пути
try:
    from core.paths import PROJECT_ROOT as _ROOT

    ROOT = Path(_ROOT)
except Exception:
    ROOT = Path(__file__).resolve().parents[2]

LOGS_DIR = ROOT / "logs"
CONFIG_DIR = ROOT / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.yml"
SETTINGS_EXAMPLE_FILE = CONFIG_DIR / "settings.example.yml"
ENV_FILE = ROOT / ".env"

# Пути к обязательным шаблонам
TPL_DIR = ROOT / "core" / "templates"
TPL_SETTINGS_EXAMPLE = TPL_DIR / "settings.example.yml"
TPL_ENV_STUB = TPL_DIR / ".env.stub.tpl"


def ensure_env_and_settings() -> None:
    # проверка наличия обязательных шаблонов
    missing_tpl = []
    if not TPL_SETTINGS_EXAMPLE.exists():
        missing_tpl.append(str(TPL_SETTINGS_EXAMPLE.relative_to(ROOT)))
    if not TPL_ENV_STUB.exists():
        missing_tpl.append(str(TPL_ENV_STUB.relative_to(ROOT)))
    if missing_tpl:
        print("[error] Отсутствуют обязательные шаблоны в core/templates:")
        for p in missing_tpl:
            print(f"  - {p}")
        print("Добавьте их в репозиторий и повторите запуск.")
        sys.exit(1)

    # логи
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # settings.example.yml
    if not SETTINGS_EXAMPLE_FILE.exists():
        SETTINGS_EXAMPLE_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TPL_SETTINGS_EXAMPLE, SETTINGS_EXAMPLE_FILE)
        print(f"[init] создан {SETTINGS_EXAMPLE_FILE.relative_to(ROOT)}")

    # settings.yml
    if not SETTINGS_FILE.exists():
        shutil.copyfile(SETTINGS_EXAMPLE_FILE, SETTINGS_FILE)
        print(f"[init] создан {SETTINGS_FILE.relative_to(ROOT)} (копия example)")

    # .env (stub) - нужен Docker/CI, чтобы сообщить путь к YAML
    if not ENV_FILE.exists():
        shutil.copyfile(TPL_ENV_STUB, ENV_FILE)
        print(f"[init] создан {ENV_FILE.relative_to(ROOT)} (stub)")
