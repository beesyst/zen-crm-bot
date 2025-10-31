from __future__ import annotations

import os
import re
import re as _re
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
    ensure_dirs,
)
from core.paths import PROJECT_ROOT as ROOT
from core.settings import get_flag, get_image
from core.tpl import (
    generate_settings_example,
    render_node_package_json,
    sync_env_from_settings,
)

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
        bufsize=0,
    )

    with open(host_log_path, "a", encoding="utf-8") as host_log_file:
        buf = ""
        try:
            while True:
                ch = proc.stdout.read(1) if proc.stdout else ""
                if ch == "" and proc.poll() is not None:
                    # процесс завершился, дольем хвост буфера
                    if buf.strip():
                        line = ansi_re.sub("", buf)
                        if ts_log_re.match(line):
                            host_log_file.write(line + "\n")
                            host_log_file.flush()
                        elif console_marker_re.match(line):
                            if echo:
                                print(line)
                        elif spinner_frame_re.match(line):
                            if echo:
                                sys.stdout.write(line + "\r")
                                sys.stdout.flush()
                        else:
                            host_log_file.write(line + "\n")
                            host_log_file.flush()
                            if echo:
                                print(line)
                    break

                if ch == "":
                    # процесс еще жив, но данных нет - продолжим цикл
                    continue

                if ch == "\r":
                    # кадр спиннера/прогресса (без \n)
                    frame = ansi_re.sub("", buf)
                    buf = ""
                    if not frame.strip():
                        continue
                    if spinner_frame_re.match(frame):
                        if echo:
                            sys.stdout.write(frame + "\r")
                            sys.stdout.flush()
                    else:
                        # это одиночная "строка без \n" - покажем как есть (в терминал), в лог не пишем
                        if echo:
                            sys.stdout.write(frame + "\r")
                            sys.stdout.flush()
                    continue

                if ch == "\n":
                    # полноценная строка
                    line = ansi_re.sub("", buf)
                    buf = ""
                    if not line.strip():
                        continue
                    if ts_log_re.match(line):
                        host_log_file.write(line + "\n")
                        host_log_file.flush()
                        continue
                    if plain_marker_re.match(line):
                        continue
                    if compose_runtime_re.match(line):
                        continue
                    if console_marker_re.match(line):
                        if echo:
                            print(line)
                        continue
                    if spinner_frame_re.match(line):
                        if echo:
                            sys.stdout.write(line + "\r")
                            sys.stdout.flush()
                        continue
                    host_log_file.write(line + "\n")
                    host_log_file.flush()
                    if echo:
                        print(line)
                    continue

                # обычный символ - копим
                buf += ch
        except KeyboardInterrupt:
            # аккуратно прервать дочерний процесс, чтобы не висел compose run
            try:
                proc.terminate()
            except Exception:
                pass
            # дочистим stdout, если что-то осталось
            try:
                tail = (proc.stdout.read() or "") if proc.stdout else ""
                if tail:
                    for line in tail.replace("\r", "\n").split("\n"):
                        line = ansi_re.sub("", line).strip()
                        if not line:
                            continue
                        if ts_log_re.match(line):
                            host_log_file.write(line + "\n")
                        elif console_marker_re.match(line):
                            if echo:
                                print(line)
                        elif not spinner_frame_re.match(line):
                            host_log_file.write(line + "\n")
                            if echo:
                                print(line)
                    host_log_file.flush()
            except Exception:
                pass
            raise


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
    return [
        "docker",
        "compose",
        "--env-file",
        str(ROOT / ".env"),
        *[arg for f in files for arg in ("-f", str(f))],
        *args,
    ]


# Чтение node_version из config/settings.yml
def _read_node_version_from_settings() -> str:
    s = _load_settings()
    return (s.get("runtime", {}) or {}).get("node_version", "")


# Одноразовая генерация core/node/package-lock.json на хосте
def ensure_node_lockfile() -> tuple[bool, str]:
    node_dir = ROOT / "core" / "node"
    pkg = node_dir / "package.json"
    lock = node_dir / "package-lock.json"

    if not pkg.exists():
        return True, "skip: core/node/package.json не найден"

    need_regen = True
    if lock.exists() and lock.stat().st_size > 0:
        # перегенерируем, только если package.json новее lockfile
        need_regen = pkg.stat().st_mtime > lock.stat().st_mtime

    if not need_regen:
        return True, "ok: package-lock.json актуален"

    node_ver = _read_node_version_from_settings() or os.environ.get("NODE_VERSION", "")
    if not node_ver:
        return False, "runtime.node_version не задан в config/settings.yml"

    image = f"node:{node_ver}-alpine"

    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{str(node_dir)}:/work",
        "-w",
        "/work",
        image,
        "sh",
        "-lc",
        # только lockfile, без postinstall-скриптов
        "npm install --package-lock-only --no-audit --no-fund --ignore-scripts",
    ]
    rc, _ = run_and_capture(cmd)
    if rc == 0 and lock.exists() and lock.stat().st_size > 0:
        return True, (
            "created: package-lock.json"
            if lock.stat().st_mtime >= pkg.stat().st_mtime
            else "updated: package-lock.json"
        )
    return False, f"npm lock generation failed (rc={rc})"


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
        print("[error] requirements.txt отсутствует (в корне проекта).")
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
        if base.exists():
            return True, ""
        return False, f"missing {base}"

    step("docker-compose files", _compose_files)


# Подготовка: очистка логов один раз, если включено в settings.yml
def _maybe_clear_logs_once():
    if get_flag("clear_logs", False):
        ensure_dirs()
        clear_all_logs()


# Подготовка: экспорт значений образов в окружение процесса для compose-вызовов
def _export_compose_env():
    os.environ["POSTGRES_IMAGE"] = get_image("postgres")
    os.environ["REDIS_IMAGE"] = get_image("redis")


def _app_image_tag() -> str:
    py = os.environ.get("PYTHON_VERSION", "").strip()
    deb = os.environ.get("PYTHON_DEBIAN", "").strip()
    node = os.environ.get("NODE_VERSION", "").strip()
    return f"zencrm-app:{py}-{deb}-{node}"


def _last_line(text: str) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1] if lines else ""


def _only_node_ver(s: str) -> str:
    m = _re.search(r"\bv?(\d+\.\d+\.\d+)\b", s)
    return f"v{m.group(1)}" if m else (s.strip() or "unknown")


def _only_pw_ver(s: str) -> str:
    m = _re.search(r"(\d+\.\d+\.\d+)", s)
    return m.group(1) if m else (s.strip() or "unknown")


# Возврат (full, repo), например ("zencrm-app:3.13.7-slim-bookworm-24.7.0", "zencrm-app").
def _find_app_image() -> tuple[str, str]:
    # попробуем через compose ps
    rc, out = run_and_capture(
        compose_cmd("ps", "--format", "{{.Image}}"), cwd=DOCKER_DIR
    )
    if rc == 0 and out:
        for line in out.splitlines():
            img = line.strip()
            if img.startswith("zencrm-app:"):
                repo, tag = img.split(":", 1)
                return img, repo

    # fallback: docker images
    rc, out = run_and_capture(
        ["docker", "images", "zencrm-app", "--format", "{{.Repository}}:{{.Tag}}"]
    )
    if rc == 0 and out:
        img = out.splitlines()[0].strip()
        if ":" in img:
            repo, tag = img.split(":", 1)
            return img, repo

    # совсем ничего не нашли
    return "zencrm-app:unknown", "zencrm-app"


# Проверка: docker / compose / (опц.) postgres-образа из settings.yml
def check_prereqs(require_postgres: bool = False):
    ok = True

    def _docker():
        rc, out = run_and_capture(["docker", "--version"])
        # "Docker version 28.3.3, build 980b856"
        if out:
            m = re.search(r"version\s+([^\s,]+)(?:,\s*build\s+([0-9a-f]+))?", out, re.I)
            if m:
                ver = m.group(1)
                build = m.group(2) or ""
                msg = f"{ver}" + (f", build {build}" if build else "")
            else:
                msg = out.strip()
        else:
            msg = "not available"
        return (rc == 0 and out), msg

    def _compose():
        rc, out = run_and_capture(["docker", "compose", "version"])
        # "Docker Compose version v2.39.1"
        if out:
            m = re.search(r"version\s+(v?\d+\.\d+\.\d+)", out, re.I)
            msg = m.group(1) if m else out.strip()
        else:
            msg = "not available"
        return (rc == 0 and out), msg

    ok &= step("Docker", _docker)
    ok &= step("Docker Compose", _compose)

    if not ok:
        print("\n[error] Docker checks failed. See logs/setup.log")
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

    # news (режим 3)
    if settings.get("modes", {}).get("news_aggregator", {}).get("enabled", False):
        HOST_LOGGER.info("compose run cli.news")
        sh_log_host(
            compose_cmd("run", "--rm", "job", "python", "-m", "cli.news"),
            cwd=DOCKER_DIR,
            echo=True,
        )


# Общий пайплайн
def _pipeline_up(*, detached: bool, run_modes: bool) -> int:
    postgres_image = os.environ.get("POSTGRES_IMAGE", "postgres:latest")
    redis_image = os.environ.get("REDIS_IMAGE", "redis:latest")

    def _prep_docker():
        # если образ приложения отсутствует - собрать; иначе сразу up
        expected = _app_image_tag()
        rc_inspect, _ = run_and_capture(["docker", "image", "inspect", expected])

        if rc_inspect != 0:
            # образа нет - один раз собираем
            rc_build = sh_log_setup(compose_cmd("build"), cwd=DOCKER_DIR)
            if rc_build != 0:
                return False, "build failed"

        args = ["up", "-d"] if detached else ["up"]
        rc_up = sh_log_setup(compose_cmd(*args), cwd=DOCKER_DIR)
        return (rc_up == 0), ("" if rc_up == 0 else "failed")

    step("Docker (pull/build/up)", _prep_docker)

    # определяем app image/repo по факту
    app_image_full, app_repo = _find_app_image()

    # версии postgres/redis - из контейнеров
    def _pg_version():
        rc, out = run_and_capture(
            compose_cmd(
                "exec", "-T", "db", "sh", "-lc", "psql --version || postgres -V"
            ),
            cwd=DOCKER_DIR,
        )
        line = _last_line(out)
        ver = ""

        # "psql (PostgreSQL) 17.6 (...)" -> "17.6 (...)", "postgres (PostgreSQL) 17.6 (...)" -> "17.6 (...)"
        if line:
            m = _re.search(r"\(PostgreSQL\)\s*(.+)$", line)  # общий случай
            if m:
                ver = m.group(1).strip()

        ok_ = rc == 0 and bool(ver)
        return ok_, (ver if ok_ else "not available")

    def _redis_version():
        rc, out = run_and_capture(
            compose_cmd(
                "exec",
                "-T",
                "redis",
                "sh",
                "-lc",
                "redis-server --version || redis-cli --version",
            ),
            cwd=DOCKER_DIR,
        )
        line = _last_line(out)
        ver = ""
        # "Redis server v=8.2.1 ..." -> "8.2.1" или "redis-cli 8.2.1" -> "8.2.1"
        m = _re.search(r"\bv=([\d\.]+)", line)
        if m:
            ver = m.group(1)
        else:
            m = _re.search(r"\b(\d+\.\d+\.\d+)\b", line)
            if m:
                ver = m.group(1)

        ok_ = rc == 0 and bool(ver)
        return ok_, (ver if ok_ else "not available")

    # сводка по образам
    def _image_app():
        tag = app_image_full.split(":", 1)[1] if ":" in app_image_full else "unknown"
        return True, tag

    def _image_pg():
        tag = postgres_image.split(":", 1)[1] if ":" in postgres_image else "latest"
        return True, tag

    def _image_redis():
        tag = redis_image.split(":", 1)[1] if ":" in redis_image else "latest"
        return True, tag

    # контейнеры
    def _containers_emit():
        rc, out = run_and_capture(
            compose_cmd("ps", "--format", "{{.Name}} {{.Image}}"),
            cwd=DOCKER_DIR,
        )
        if rc != 0 or not out:
            return False, "not available"

        lines = [l.strip() for l in out.splitlines() if l.strip()]

        # порядок: db, redis, api, worker, beat
        prio = {
            "docker-db-1": 0,
            "docker-redis-1": 1,
            "docker-api-1": 2,
            "docker-worker-1": 3,
            "docker-beat-1": 4,
        }
        parsed = []
        for line in lines:
            try:
                name, image = line.split(" ", 1)
                parsed.append((prio.get(name, 100), name, image))
            except ValueError:
                continue

        for _, name, image in sorted(parsed, key=lambda x: (x[0], x[1])):
            # хотим ровно: [ok] container - <name> - <image>
            step(f"container - {name}", lambda n=name, i=image: (True, f"- {i}"))

        return True, ""

    # node/playwright из образа приложения - указываем repo (без тега)
    def _node_version():
        rc, out = run_and_capture(
            compose_cmd("exec", "-T", "api", "sh", "-lc", "node -v"),
            cwd=DOCKER_DIR,
        )
        ver = _only_node_ver(_last_line(out))
        ok_ = rc == 0 and ver != "unknown"
        return ok_, f"{ver} — image {app_repo}"

    def _playwright_version():
        # быстрая проверка: бинарь playwright в path в job
        rc1, out1 = run_and_capture(
            compose_cmd(
                "run",
                "--rm",
                "job",
                "sh",
                "-lc",
                "command -v playwright >/dev/null 2>&1 && playwright --version || true",
            ),
            cwd=DOCKER_DIR,
        )
        m1 = _re.search(r"(\d+\.\d+\.\d+)", out1 or "")
        if rc1 == 0 and m1:
            return True, f"{m1.group(1)} - image zencrm-app"

        # фолбэк: через npx (если по какой-то причине path не содержит симлинк)
        rc2, out2 = run_and_capture(
            compose_cmd(
                "run",
                "--rm",
                "job",
                "sh",
                "-lc",
                "npx --yes playwright --version || true",
            ),
            cwd=DOCKER_DIR,
        )
        m2 = _re.search(r"(\d+\.\d+\.\d+)", out2 or "")
        if rc2 == 0 and m2:
            return True, f"{m2.group(1)} - image zencrm-app"

        return False, "not installed - image zencrm-app"

    # печатаем в желаемом порядке
    step("image - zencrm-app", _image_app)
    step("image - postgres", _image_pg)
    step("image - redis", _image_redis)
    _containers_emit()
    step("PostgreSQL", _pg_version)
    step("Redis", _redis_version)
    step("Node", _node_version)
    step("Playwright", _playwright_version)

    if run_modes:
        _run_modes_after_up()
    return 0


# Общий раннер для оберткки
def _run_stack(*, detached: bool, run_modes: bool) -> None:
    print("Start")
    check_required_files()
    _maybe_clear_logs_once()
    ensure_files()
    generate_settings_example()
    render_node_package_json()
    _export_compose_env()
    sync_env_from_settings()
    check_prereqs(require_postgres=False)
    step("package-lock.json", ensure_node_lockfile)

    rc = _pipeline_up(detached=detached, run_modes=run_modes)
    print("Finish" if rc == 0 else "Finish (with errors — see logs/setup.log)")
    sys.exit(rc)


# Команда: локальный запуск в форграунде (build + up)
def cmd_dev():
    _run_stack(detached=False, run_modes=False)


# Команада: локальный запуск в фоне (build + up -d)
def cmd_dev_bg():
    _run_stack(detached=True, run_modes=True)


# Команада: продовый запуск в фоне (build + up -d)
def cmd_prod_up():
    _run_stack(detached=True, run_modes=True)


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
