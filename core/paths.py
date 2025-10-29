from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
LOGS_DIR = PROJECT_ROOT / "logs"
TEMPLATES_DIR = PROJECT_ROOT / "app" / "templates"
CORE_TEMPLATES_DIR = PROJECT_ROOT / "core" / "templates"
STORAGE_DIR = PROJECT_ROOT / "storage"
STORAGE_PROJECTS = STORAGE_DIR / "projects"
MAIN_TEMPLATE = CORE_TEMPLATES_DIR / "main_template.json"
CELERY_DIR = STORAGE_DIR / "celery"
NODE_DIR = PROJECT_ROOT / "core" / "node"
NODE_PKG = NODE_DIR / "package.json"
NODE_LOCK = NODE_DIR / "package-lock.json"
PLAYWRIGHT_CACHE = NODE_DIR / ".ms-playwright"

# Единое место - фракции логов
LOG_PATHS = {
    "host": LOGS_DIR / "host.log",
    "setup": LOGS_DIR / "setup.log",
    "kommo": LOGS_DIR / "kommo.log",
    "news": LOGS_DIR / "news.log",
}


def ensure_dirs():
    for p in (LOGS_DIR, STORAGE_DIR, CELERY_DIR, STORAGE_PROJECTS):
        p.mkdir(parents=True, exist_ok=True)
