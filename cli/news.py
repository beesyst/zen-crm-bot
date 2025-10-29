from __future__ import annotations

from core.console import error, finish, ok
from core.log_setup import get_logger, setup_logging
from core.news.runner import run_news_once


def main():
    setup_logging(level="INFO", service="zen-crm", env="dev", write_files=True)
    log = get_logger("news")
    try:
        ok("start news")
        res = run_news_once()
        log.info("cli.news finished: %s", res)
        total = int(res.get("saved", 0)) + int(res.get("skipped", 0))
        ok(f"total: {total}")
        finish()
    except Exception as e:
        error("news", str(e))
        raise


if __name__ == "__main__":
    main()
