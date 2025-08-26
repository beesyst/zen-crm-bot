from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from textwrap import dedent

PROJECT_ROOT_FALLBACK = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT_FALLBACK) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT_FALLBACK))

from core.log_setup import clear_all_logs, get_logger
from core.paths import LOG_PATHS, ensure_dirs
from core.paths import PROJECT_ROOT as ROOT
from core.settings import get_flag, get_image

SETUP_LOGGER = get_logger("setup")

# Пути и константы
DOCKER_DIR = ROOT / "docker"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
ENV_TPL_FILE = ROOT / "core" / "templates" / ".env.stub.tpl"
LOG_FILE = LOG_PATHS["setup"]

# Кадры спиннера
_SPIN = ["|", "/", "-", "\\"]


# Из settings.yml
def _export_compose_env():
    os.environ["POSTGRES_IMAGE"] = get_image("postgres")
    os.environ["REDIS_IMAGE"] = get_image("redis")


# Стрим вывода в терминал (для коротких команд типа curl/logs)
def sh_stream(cmd: list[str], cwd: Path | None = None) -> int:
    print(f"$ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(cwd) if cwd else None)


# Запись stdout/stderr в logs/setup.log (без вывода в терминал)
def sh_log(cmd: list[str], cwd: Path | None = None) -> int:
    ensure_dirs()
    SETUP_LOGGER.info("$ %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in proc.stdout or []:
        line = line.rstrip("\n")
        if line:
            SETUP_LOGGER.info(line)
    proc.wait()
    rc = proc.returncode
    if rc != 0:
        SETUP_LOGGER.error("exit code: %s", rc)
    return rc


# Выполнить команду, получить (rc, stdout_text) и продублировать вывод в setup.log
def run_and_capture(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    ensure_dirs()
    SETUP_LOGGER.info("$ %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        msg = f"command not found: {cmd[0]}"
        SETUP_LOGGER.error(msg)
        return 127, msg

    lines: list[str] = []
    for line in proc.stdout or []:
        line = line.rstrip("\n")
        if line:
            lines.append(line)
            SETUP_LOGGER.info(line)
    proc.wait()
    rc = proc.returncode
    if rc != 0:
        SETUP_LOGGER.error("exit code: %s", rc)
    return rc, "\n".join(lines).strip()


# Визуализация статуса (спиннер)
def spinner_run(label: str, worker: callable) -> bool:
    done = threading.Event()
    state = {"ok": False, "label": label, "suffix": ""}

    def spin():
        i = 0
        while not done.is_set():
            sym = _SPIN[i % len(_SPIN)]
            print(f"\r[{sym}] {state['label']}", end="", flush=True)
            i += 1
            time.sleep(0.15)

    t = threading.Thread(target=spin, daemon=True)
    t.start()
    try:
        ok, suffix = worker()
        state["ok"] = bool(ok)
        state["suffix"] = (suffix or "").strip()
    except Exception:
        state["ok"] = False
        state["suffix"] = ""
    finally:
        done.set()
        t.join()
        prefix = "[ok]" if state["ok"] else "[error]"
        tail = f" {state['suffix']}" if state["suffix"] else ""
        print(f"\r{prefix} {state['label']}{tail}{' ' * 40}")
    return state["ok"]


# Короткие хелперы статусов (без спиннера)
def step_ok(text: str):
    print(f"[ok] {text}")


def step_error(text: str):
    print(f"[error] {text}")


# Проверки наличия обязательных файлов
def check_required_files():
    req = ROOT / "requirements.txt"
    if not req.exists():
        print("[error] requirements.txt отсутствует (zen-crm/requirements.txt).")
        sys.exit(1)


# Подготовка базовых файлов
def ensure_files():
    ensure_dirs()
    DOCKER_DIR.mkdir(parents=True, exist_ok=True)

    # .env.example из core/templates/env.example.tpl
    def _env_example():
        SETUP_LOGGER.info(".env.example path: %s", ENV_EXAMPLE)
        SETUP_LOGGER.info("template path:     %s", ENV_TPL_FILE)
        if ENV_EXAMPLE.exists():
            return True, ""
        if ENV_TPL_FILE.exists():
            try:
                shutil.copyfile(ENV_TPL_FILE, ENV_EXAMPLE)
                return True, "created from core/templates/env.example.tpl"
            except Exception as e:
                run_and_capture(
                    ["/bin/sh", "-c", f"echo 'copy .env.example failed: {e}' 1>&2"]
                )
                return False, "copy failed"
        return False, f"missing template {ENV_TPL_FILE}"

    spinner_run(".env.example", _env_example)

    # .env (копия .env.example)
    def _env():
        SETUP_LOGGER.info(".env path: %s", ENV_FILE)
        if ENV_FILE.exists():
            return True, ""
        try:
            shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
            return True, "created from .env.example"
        except Exception as e:
            run_and_capture(["/bin/sh", "-c", f"echo 'copy .env failed: {e}' 1>&2"])
            return False, "copy failed"

    spinner_run(".env", _env)

    # проверка config/settings.yml
    def _settings():
        settings_yml = ROOT / "config" / "settings.yml"
        if settings_yml.exists():
            return True, ""
        return False, f"missing at {settings_yml}"

    spinner_run("config/settings.yml", _settings)

    # проверка docker-compose файлов
    def _compose_files():
        base = DOCKER_DIR / "docker-compose.yml"
        override = DOCKER_DIR / "docker-compose.override.yml"
        ok = base.exists() and override.exists()
        return (ok, "" if ok else f"missing {base if not base.exists() else override}")

    spinner_run("docker-compose files", _compose_files)


# Принудительная регенерация compose
def regen_compose():
    print("[skip] compose templates managed in repo — nothing to regenerate")


# Проверка Docker / Compose / Postgres (с версиями)
def check_prereqs(require_postgres: bool = True):
    ok = True

    def _docker():
        rc, out = run_and_capture(["docker", "--version"])
        if rc == 0 and out:
            return True, _suffix(out, "Docker")
        return False, "not available"

    def _compose():
        rc, out = run_and_capture(["docker", "compose", "version"])
        if rc == 0 and out:
            return True, _suffix(out, "Docker Compose")
        return False, "not available"

    ok &= spinner_run("Docker", _docker)
    ok &= spinner_run("Docker Compose", _compose)

    if require_postgres:

        def _postgres():
            pg_image = get_image("postgres")
            rc, out = run_and_capture(
                ["docker", "run", "--rm", "--pull=missing", pg_image, "postgres", "-V"]
            )
            if rc == 0 and out:
                line = (out or "").splitlines()[-1].strip()
                return True, _suffix(line, "postgres")
            return False, "not available"

        ok &= spinner_run("Postgres", _postgres)

    if not ok:
        print("\n[error] Docker/Postgres checks failed. See logs/setup.log")
        sys.exit(1)


def _suffix(out: str, expected_prefix: str) -> str:
    low = out.strip()
    pref = expected_prefix.strip() + " "
    if low.lower().startswith(pref.lower()):
        return low[len(pref) :].strip()
    return low


# Сборка команды docker compose
def compose_cmd(*args: str) -> list[str]:
    files = [DOCKER_DIR / "docker-compose.yml"]
    override = DOCKER_DIR / "docker-compose.override.yml"
    if override.exists():
        files.append(override)

    return [
        "docker",
        "compose",
        *[arg for f in files for arg in ("-f", str(f))],
        *args,
    ]


def _maybe_clear_logs_once():
    if get_flag("clear_logs", False):
        ensure_dirs()
        clear_all_logs()


# Команды запуска/остановки
def cmd_dev():
    print("Start")
    check_required_files()
    ensure_files()
    _maybe_clear_logs_once()
    _export_compose_env()
    check_prereqs()
    rc = sh_log(compose_cmd("up", "--build"), cwd=DOCKER_DIR)
    print("Finish" if rc == 0 else "Finish (with errors — see logs/setup.log)")
    sys.exit(rc)


# Запуск стека в фоне (build + up -d) с прогрессом и Start/Finish.
def cmd_dev_bg():
    print("Start")
    check_required_files()
    ensure_files()
    _maybe_clear_logs_once()
    _export_compose_env()
    check_prereqs()

    def _up():
        rc = sh_log(compose_cmd("up", "-d", "--build"), cwd=DOCKER_DIR)
        return (rc == 0), ""

    if not spinner_run("docker compose up (detached)", _up):
        step_error("compose up failed")
        sys.exit(1)

    print("Finish")
    sys.exit(0)


# Продовый запуск в фоне (build + up -d) с прогрессом и Start/Finish
def cmd_prod_up():
    print("Start")
    check_required_files()
    ensure_files()
    _maybe_clear_logs_once()
    _export_compose_env()
    check_prereqs()

    def _up_bg():
        rc = sh_log(compose_cmd("up", "-d", "--build"), cwd=DOCKER_DIR)
        return (rc == 0), ""

    if not spinner_run("docker compose up (detached)", _up_bg):
        step_error("compose up failed")
        sys.exit(1)

    print("Finish")
    sys.exit(0)


# Продовое выключение сервиса (compose down) с прогрессом и Start/Finish
def cmd_prod_down():
    print("Start")
    _export_compose_env()
    check_prereqs(require_postgres=False)

    def _down():
        rc = sh_log(compose_cmd("down"), cwd=DOCKER_DIR)
        return (rc == 0), ""

    if not spinner_run("docker compose down", _down):
        step_error("compose down failed")
        sys.exit(1)
    print("Finish")
    sys.exit(0)


# Локальная остановка сервиса (compose down) с записью в setup.log
def cmd_stop():
    _export_compose_env()
    rc = sh_log(compose_cmd("down"), cwd=DOCKER_DIR)
    sys.exit(rc)


# Текущие логи docker compose (tail -f) - стрим в терминал
def cmd_logs():
    _export_compose_env()
    check_prereqs(require_postgres=False)
    sys.exit(sh_stream(compose_cmd("logs", "-f", "--tail=200"), cwd=DOCKER_DIR))


# Проверка /health локального API - вывод в терминал
def cmd_health():
    sys.exit(sh_stream(["curl", "-sS", "http://localhost:8000/health"]))


# Отправка тестового вебхука Kommo - вывод в терминал (короткая команда)
def cmd_test_webhook():
    payload = '{"lead_id":12345,"stage":"READY_FOR_OUTREACH","fields":{"name":"Demo","website":"https://example.org"}}'
    sys.exit(
        sh_stream(
            [
                "curl",
                "-sS",
                "-X",
                "POST",
                "http://localhost:8000/webhooks/kommo/lead.updated",
                "-H",
                "Content-Type: application/json",
                "-d",
                payload,
            ]
        )
    )


def cmd_prune():
    run_and_capture(["docker", "image", "prune", "-f"])
    rc, _ = run_and_capture(["docker", "builder", "prune", "-f"])
    sys.exit(rc)


# Справка по командам
def help_and_exit():
    print(
        dedent(
            """\
        Usage: python3 config/start.py <command>
          (если команда не указана — `dev-bg`)

          dev             — запустить стек в форграунде (build + up)
          dev-bg          — запустить стек в фоне (build + up -d)
          stop            — остановить локальный стек (down)
          logs            — хвост логов всех сервисов
          health          — проверить API /health
          test-webhook    — послать тестовый вебхук lead.updated
          prod-up         — сборка и запуск в фоне (для сервера)
          prod-down       — остановка на сервере
          regen-compose   — (no-op) шаблоны управляются в репозитории
          prune           — очистить dangling-образы и build-кеш

        Требуется существующий файл:
          requirements.txt — единственный источник зависимостей
    """
        )
    )
    sys.exit(0)


# Разбор аргумента команды и диспетчеризация
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dev-bg"
    if cmd == "dev":
        cmd_dev()
    elif cmd == "dev-bg":
        cmd_dev_bg()
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "logs":
        cmd_logs()
    elif cmd == "health":
        cmd_health()
    elif cmd == "test-webhook":
        cmd_test_webhook()
    elif cmd == "prod-up":
        cmd_prod_up()
    elif cmd == "prod-down":
        cmd_prod_down()
    elif cmd == "regen-compose":
        regen_compose()
    else:
        help_and_exit()


if __name__ == "__main__":
    main()
