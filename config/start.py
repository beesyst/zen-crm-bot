from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import yaml

# Корень проекта в sys.path для локальных импортов
PROJECT_ROOT_FALLBACK = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT_FALLBACK) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT_FALLBACK))

# Локальные импорты инфраструктуры
from core.console import step
from core.log_setup import clear_all_logs, get_logger
from core.paths import (
    LOG_PATHS,
    NODE_DIR,
    NODE_LOCK,
    NODE_PKG,
    PLAYWRIGHT_CACHE,
    ensure_dirs,
)
from core.paths import PROJECT_ROOT as ROOT
from core.settings import get_flag, get_image
from core.tpl import (
    generate_settings_example,
    render_node_package_json,
    sync_env_from_settings,
)

# Переменные окружения для playwright/node
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PLAYWRIGHT_CACHE)
os.environ["NODE_PATH"] = str(NODE_DIR / "node_modules")

SETUP_LOGGER = get_logger("setup")
HOST_LOGGER = get_logger("host")

# Пути и константы
DOCKER_DIR = ROOT / "docker"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
ENV_TPL_FILE = ROOT / "core" / "templates" / ".env.stub.tpl"


# Shell: выполнение команды, стрим вывода в терминал
def sh_stream(cmd: list[str], cwd: Path | None = None) -> int:
    print(f"$ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(cwd) if cwd else None)


# Shell: выполнение команды и запись stdout/stderr в logs/setup.log
def sh_log_setup(cmd: list[str], cwd: Path | None = None) -> int:
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


# Shell: выполнение команды и запись stdout/stderr в logs/host.log
def sh_log_host(cmd: list[str], cwd: Path | None = None, echo: bool = False) -> int:
    ensure_dirs()
    HOST_LOGGER.info("$ %s", " ".join(cmd))

    # паттерны
    ts_log_re = re.compile(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[[A-Z]+\] - \[[^\]]+\] "
    )
    console_marker_re = re.compile(r"^\[(skip|add|update|ok|error)\b", re.IGNORECASE)
    plain_marker_re = re.compile(r"^(Start|Finish)$")
    spinner_frame_re = re.compile(r"^\r?\[(\||/|-|\\)\] ")
    compose_runtime_re = re.compile(
        r"^ ?Container .*  (Running|Started|Starting|Created|Recreated|Healthy|Built)$"
    )
    # ANSI CSI: \x1B[ ...  (убираем управляющие последовательности, в т.ч. возможные '\x1b[K')
    ansi_re = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

    host_log_path = Path(LOG_PATHS["host"]).resolve()

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    with open(host_log_path, "a", encoding="utf-8") as host_log_file:
        for raw in proc.stdout or []:
            # первичная нормализация
            line = raw.rstrip("\n")
            if not line:
                continue

            # удалить CR (кадры спиннера печатаются с '\r') и ANSI ESC-последовательности
            line = ansi_re.sub("", line)

            if not line:
                continue

            # уже отформатированные (с таймстампом и [name]) → только в host.log
            if ts_log_re.match(line):
                host_log_file.write(line + "\n")
                host_log_file.flush()
                continue

            # кадры спиннера вида "[|] msg", "[/] msg", "[-] msg", "[\] msg" — показываем в терминале без \n
            if spinner_frame_re.match(line):
                if echo:
                    sys.stdout.write(line + "\r")
                    sys.stdout.flush()
                continue

            # "Start"/"Finish" из ВНУТРЕННЕГО процесса — подавляем целиком (и из echo, и из host.log)
            if plain_marker_re.match(line):
                continue

            # служебные сообщения docker compose - игнор
            if compose_runtime_re.match(line):
                continue

            # короткие статусы [ok]/[skip]/[add]/[update]/[error] → только терминал (без логгера)
            if console_marker_re.match(line):
                if echo:
                    print(line)
                continue

            # все остальное → host.log (+ терминал при echo=True)
            host_log_file.write(line + "\n")
            host_log_file.flush()
            if echo:
                print(line)

    proc.wait()
    rc = proc.returncode
    if rc != 0:
        HOST_LOGGER.error("exit code: %s", rc)
    return rc


# Shell: выполнение команды и возврат (rc, stdout_text), лог в setup.log
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


# Сервис: загрузка config/settings.yml (для чтения режимов и т.п.)
def _load_settings() -> dict:
    p = ROOT / "config" / "settings.yml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}


# Сервис: постройка команды docker compose с корректным набором файлов
def compose_cmd(*args: str) -> list[str]:
    files = [DOCKER_DIR / "docker-compose.yml"]
    override = DOCKER_DIR / "docker-compose.override.yml"
    if override.exists():
        files.append(override)
    return [
        "docker",
        "compose",
        "--env-file",
        str(ROOT / ".env"),
        *[arg for f in files for arg in ("-f", str(f))],
        *args,
    ]


# Сервис: форматирование суффикса версии/баннера из stdout
def _suffix(out: str, expected_prefix: str) -> str:
    low = out.strip()
    pref = expected_prefix.strip() + " "
    if low.lower().startswith(pref.lower()):
        return low[len(pref) :].strip()
    return low


# Подготовка: проверка наличия критичных файлов (requirements и т.д.)
def check_required_files():
    req = ROOT / "requirements.txt"
    if not req.exists():
        print("[error] requirements.txt отсутствует (zen-crm/requirements.txt).")
        sys.exit(1)


# Подготовка: создание базовых файлов (.env.example, .env) и проверка конфигов
def ensure_files():
    ensure_dirs()
    DOCKER_DIR.mkdir(parents=True, exist_ok=True)

    # .env.example из core/templates/.env.stub.tpl
    def _env_example():
        SETUP_LOGGER.info(".env.example path: %s", ENV_EXAMPLE)
        SETUP_LOGGER.info("template path:     %s", ENV_TPL_FILE)
        if ENV_EXAMPLE.exists():
            return True, ""
        if ENV_TPL_FILE.exists():
            try:
                shutil.copyfile(ENV_TPL_FILE, ENV_EXAMPLE)
                return True, "created from core/templates/.env.stub.tpl"
            except Exception as e:
                run_and_capture(
                    ["/bin/sh", "-c", f"echo 'copy .env.example failed: {e}' 1>&2"]
                )
                return False, "copy failed"
        return False, f"missing template {ENV_TPL_FILE}"

    step(".env.example", _env_example)

    # .env (копия .env.example), дальше sync_env_from_settings() допишет нужные пары
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

    step(".env", _env)

    # проверка наличия config/settings.yml
    def _settings():
        settings_yml = ROOT / "config" / "settings.yml"
        if settings_yml.exists():
            return True, ""
        return False, f"missing at {settings_yml}"

    step("config/settings.yml", _settings)

    # проверка compose-файлов
    def _compose_files():
        base = DOCKER_DIR / "docker-compose.yml"
        override = DOCKER_DIR / "docker-compose.override.yml"
        ok = base.exists() and override.exists()
        return (ok, "" if ok else f"missing {base if not base.exists() else override}")

    step("docker-compose files", _compose_files)


# Подготовка: очистка логов один раз, если включено в settings.yml
def _maybe_clear_logs_once():
    if get_flag("clear_logs", False):
        ensure_dirs()
        clear_all_logs()


# Подготовка: установка node/npm и playwright браузера в кеш проекта
def ensure_node_deps():
    # если нет package.json - ничего не делаем
    if not NODE_PKG.exists():
        return True, "no core/node/package.json — skip"

    # пытаемся использовать хостовый node/npm
    rc_node, _ = run_and_capture(["node", "--version"])
    rc_npm, _ = run_and_capture(["npm", "--version"])
    if rc_node != 0 or rc_npm != 0:
        return True, "host node/npm absent — will be installed inside Docker"

    # если есть - поставим локально (ускорит npx в Docker build cache)
    if NODE_LOCK.exists():
        rc, _ = run_and_capture(["npm", "ci"], cwd=NODE_DIR)
    else:
        rc, _ = run_and_capture(["npm", "install", "--no-fund"], cwd=NODE_DIR)
    if rc != 0:
        return True, "host npm install failed — continue (Docker will handle)"

    # попробуем поставить браузер кешом
    rc, _ = run_and_capture(
        ["npm", "run", "playwright:install", "--silent"],
        cwd=NODE_DIR,
    )
    if rc != 0:
        run_and_capture(["npx", "playwright", "install", "chromium"], cwd=NODE_DIR)

    return True, ""


# Подготовка: экспорт значений образов в окружение процесса для compose-вызовов
def _export_compose_env():
    os.environ["POSTGRES_IMAGE"] = get_image("postgres")
    os.environ["REDIS_IMAGE"] = get_image("redis")


# Проверка: docker / compose / (опц.) postgres-образа из settings.yml
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

    ok &= step("Docker", _docker)
    ok &= step("Docker Compose", _compose)

    if require_postgres:

        def _postgres():
            # берем образ из settings.yml (через core.settings.get_image)
            pg_image = get_image("postgres")
            rc, out = run_and_capture(
                ["docker", "run", "--rm", "--pull=missing", pg_image, "postgres", "-V"]
            )
            if rc == 0 and out:
                line = (out or "").splitlines()[-1].strip()
                return True, _suffix(line, "postgres")
            return False, "not available"

        ok &= step("Postgres", _postgres)

    if not ok:
        print("\n[error] Docker/Postgres checks failed. See logs/setup.log")
        sys.exit(1)


# Режимы: после успешного up - условный запуск research/enrich CLI
def _run_modes_after_up():
    settings = _load_settings()

    # research (режим 1)
    if settings.get("modes", {}).get("research_and_intake", {}).get("enabled", False):
        HOST_LOGGER.info("compose run cli.research")
        sh_log_host(
            compose_cmd("run", "--rm", "job", "python", "-m", "cli.research"),
            cwd=DOCKER_DIR,
            echo=True,
        )

    # enrich (режим 2)
    if settings.get("modes", {}).get("enrich_existing", {}).get("enabled", False):
        HOST_LOGGER.info("compose run cli.enrich")
        sh_log_host(
            compose_cmd("run", "--rm", "job", "python", "-m", "cli.enrich"),
            cwd=DOCKER_DIR,
            echo=True,
        )


# Команда: локальный запуск в форграунде (build + up)
def cmd_dev():
    print("Start")
    check_required_files()
    _maybe_clear_logs_once()
    ensure_files()
    generate_settings_example()
    render_node_package_json()
    step("Node deps (npm + playwright)", ensure_node_deps)
    _export_compose_env()
    sync_env_from_settings()
    check_prereqs()
    rc = sh_log_setup(compose_cmd("up", "--build"), cwd=DOCKER_DIR)
    print("Finish" if rc == 0 else "Finish (with errors — see logs/setup.log)")
    sys.exit(rc)


# Команада: локальный запуск в фоне (build + up -d)
def cmd_dev_bg():
    print("Start")
    check_required_files()
    _maybe_clear_logs_once()
    ensure_files()
    generate_settings_example()
    render_node_package_json()
    step("Node deps (npm + playwright)", ensure_node_deps)
    _export_compose_env()
    sync_env_from_settings()
    check_prereqs()

    def _up():
        rc = sh_log_setup(compose_cmd("up", "-d", "--build"), cwd=DOCKER_DIR)
        return (rc == 0), ("" if rc == 0 else "compose up failed")

    if not step("docker compose up (detached)", _up):
        sys.exit(1)

    _run_modes_after_up()
    print("Finish")
    sys.exit(0)


# Команада: продовый запуск в фоне (build + up -d)
def cmd_prod_up():
    print("Start")
    check_required_files()
    _maybe_clear_logs_once()
    ensure_files()
    generate_settings_example()
    render_node_package_json()  # <-- раньше
    step("Node deps (npm + playwright)", ensure_node_deps)
    _export_compose_env()
    sync_env_from_settings()
    check_prereqs()

    def _up_bg():
        rc = sh_log_setup(compose_cmd("up", "-d", "--build"), cwd=DOCKER_DIR)
        return (rc == 0), ("" if rc == 0 else "compose up failed")

    if not step("docker compose up (detached)", _up_bg):
        sys.exit(1)

    _run_modes_after_up()
    print("Finish")
    sys.exit(0)


# Команада: разовый запуск research CLI внутри job-контейнера
def cmd_run_research():
    _export_compose_env()
    check_prereqs(require_postgres=False)
    sys.exit(
        sh_log_host(
            compose_cmd("run", "--rm", "job", "python", "-m", "cli.research"),
            cwd=DOCKER_DIR,
            echo=True,
        )
    )


# Команада: разовый запуск enrich CLI внутри job-контейнера
def cmd_run_enrich():
    _export_compose_env()
    check_prereqs(require_postgres=False)
    sys.exit(
        sh_log_host(
            compose_cmd("run", "--rm", "job", "python", "-m", "cli.enrich"),
            cwd=DOCKER_DIR,
            echo=True,
        )
    )


# Команада: продовое выключение стека (down)
def cmd_prod_down():
    print("Start")
    _export_compose_env()
    check_prereqs(require_postgres=False)

    def _down():
        rc = sh_log_setup(compose_cmd("down"), cwd=DOCKER_DIR)
        return (rc == 0), ("" if rc == 0 else "compose down failed")

    if not step("docker compose down", _down):
        sys.exit(1)

    print("Finish")
    sys.exit(0)


# Команада: локальная остановка стека (down)
def cmd_stop():
    _export_compose_env()
    rc = sh_log_setup(compose_cmd("down"), cwd=DOCKER_DIR)
    sys.exit(rc)


# Команада: текущие логи всех сервисов (tail -f)
def cmd_logs():
    _export_compose_env()
    check_prereqs(require_postgres=False)
    sys.exit(sh_stream(compose_cmd("logs", "-f", "--tail=200"), cwd=DOCKER_DIR))


# Команада: проверка локального API по /health
def cmd_health():
    sys.exit(sh_stream(["curl", "-sS", "http://localhost:8000/health"]))


# Команада: отправка тестового вебхука Kommo
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


# Команада: очистка dangling-образов и build-кеша
def cmd_prune():
    run_and_capture(["docker", "image", "prune", "-f"])
    rc, _ = run_and_capture(["docker", "builder", "prune", "-f"])
    sys.exit(rc)


# Команада: печать справки и выход
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
          run-research    — одноразовый запуск режима 1 (research)
          run-enrich      — одноразовый запуск режима 2 (enrich)

        Требуется существующий файл:
          requirements.txt — единственный источник зависимостей

        Примечание: dev-bg / prod-up после поднятия стека автоматически запускают включенные в config/settings.yml режимы (research/enrich).
    """
        )
    )
    sys.exit(0)


# Entrypoint: диспетчер команд
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
        print("[skip] compose templates managed in repo - nothing to regenerate")
        sys.exit(0)
    elif cmd == "run-research":
        cmd_run_research()
    elif cmd == "run-enrich":
        cmd_run_enrich()
    else:
        help_and_exit()


if __name__ == "__main__":
    main()
