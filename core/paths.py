from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
LOGS_DIR = PROJECT_ROOT / "logs"
TEMPLATES_DIR = PROJECT_ROOT / "app" / "templates"
STORAGE_DIR = PROJECT_ROOT / "storage"

# Единое место - фракции логов
LOG_PATHS = {
    "host": LOGS_DIR / "host.log",
    "api": LOGS_DIR / "api.log",
    "worker": LOGS_DIR / "worker.log",
    "email": LOGS_DIR / "email.log",
    "discord": LOGS_DIR / "discord.log",
    "telegram": LOGS_DIR / "telegram.log",
    "docker": LOGS_DIR / "docker.log",
    "setup": LOGS_DIR / "setup.log",
    "all": LOGS_DIR / "zen-crm.log",
}


def ensure_dirs():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
