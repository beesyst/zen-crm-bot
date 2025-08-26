# zen-crm/core/install.py
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Централизованные пути
try:
    from core.paths import PROJECT_ROOT as _ROOT

    ROOT = Path(_ROOT)
except Exception:
    ROOT = Path(__file__).resolve().parents[1]

DOCKER_DIR = ROOT / "docker"
LOGS_DIR = ROOT / "logs"
REQUIREMENTS_TXT = ROOT / "requirements.txt"

# Импорт тонкого сетапа env/settings из шаблонов
from core.bootstrap.env_setup import ensure_env_and_settings


def sh(cmd: list[str], cwd: Path | None = None) -> int:
    """Запустить shell-команду и стримить вывод."""
    print(f"$ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(cwd) if cwd else None)


# Проверка обязательных файлов репозитория (Docker и requirements)
def check_repo_files() -> None:
    missing = []
    if not (DOCKER_DIR / "Dockerfile").exists():
        missing.append("docker/Dockerfile")
    if not (DOCKER_DIR / "docker-compose.yml").exists():
        missing.append("docker/docker-compose.yml")
    if not (DOCKER_DIR / "docker-compose.override.yml").exists():
        missing.append("docker/docker-compose.override.yml")
    if not REQUIREMENTS_TXT.exists():
        missing.append("requirements.txt")

    if missing:
        print("[error] Отсутствуют обязательные файлы:")
        for f in missing:
            print(f"  - {f}")
        print("Добавь их в репозиторий и повтори запуск.")
        sys.exit(1)


# Проверка наличия Docker/Compose
def check_docker() -> None:
    rc1 = sh(["docker", "--version"])
    rc2 = sh(["docker", "compose", "version"])
    if rc1 != 0 or rc2 != 0:
        print("\n[error] Нужен установленный Docker и Docker Compose.")
        sys.exit(1)


# Опциональная локальная установка без Docker (для dev окружения)
def install_local_venv(
    venv_dir: Path | None = None, with_playwright_browsers: bool = False
) -> None:
    venv_dir = venv_dir or (ROOT / ".venv")
    if not venv_dir.exists():
        print(f"[install] Создаю виртуальное окружение: {venv_dir}")
        rc = sh([sys.executable, "-m", "venv", str(venv_dir)])
        if rc != 0:
            sys.exit(rc)

    pip_bin = venv_dir / "bin" / "pip"
    python_bin = venv_dir / "bin" / "python"

    if not pip_bin.exists():
        print("[error] Похоже, venv создался некорректно (нет bin/pip).")
        sys.exit(1)

    if not REQUIREMENTS_TXT.exists():
        print("[error] requirements.txt отсутствует.")
        sys.exit(1)

    print("[install] Устанавливаю Python-зависимости из requirements.txt ...")
    rc = sh([str(pip_bin), "install", "-r", str(REQUIREMENTS_TXT)])
    if rc != 0:
        sys.exit(rc)

    if with_playwright_browsers:
        print("[install] Устанавливаю браузеры Playwright (локально) ...")
        rc = sh([str(python_bin), "-m", "playwright", "install", "chromium"])
        if rc != 0:
            sys.exit(rc)


# Единая точка входа для start.py: подготовка env/settings и проверок
def bootstrap(docker_required: bool = True) -> None:
    ensure_env_and_settings()
    check_repo_files()
    if docker_required:
        check_docker()


# CLI: python -m core.install [--local] [--local-with-browsers]
def main():
    args = set(sys.argv[1:])
    if "--local" in args or "--local-with-browsers" in args:
        ensure_env_and_settings()
        check_repo_files()
        install_local_venv(with_playwright_browsers="--local-with-browsers" in args)
        print("[ok] Локальная установка завершена.")
        sys.exit(0)

    # по умолчанию docker-first bootstrap
    bootstrap(docker_required=True)
    print("[ok] Bootstrap проверок/инициализации завершён.")


if __name__ == "__main__":
    main()
