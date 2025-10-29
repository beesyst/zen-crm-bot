from celery import Celery
from core.log_setup import get_logger, setup_logging
from core.news.runner import run_news_once
from core.settings import get_settings

setup_logging(level="INFO", service="zen-crm", env="dev", write_files=True)
log = get_logger("news")

cfg = get_settings()
REDIS_URL = cfg["infra"]["redis_url"]
celery = Celery("news", broker=REDIS_URL, backend=REDIS_URL)
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)


@celery.task(name="news.pull_all")
def news_pull_all():
    return run_news_once()


# Расписание из modes.news_aggregator.schedule
mode = (cfg.get("modes") or {}).get("news_aggregator") or {}
sch = mode.get("schedule") or {}

slack_sec = int(sch.get("slack_pull") or 300)
twitter_sec = int(sch.get("twitter_pull") or 600)
rss_sec = int(sch.get("rss_pull") or 600)

# Простой вариант - пулим все одним таском с минимальным интервалом
celery.conf.beat_schedule = {
    "news-pull": {
        "task": "news.pull_all",
        "schedule": min(slack_sec, twitter_sec, rss_sec),
    }
}
