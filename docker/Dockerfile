# syntax=docker/dockerfile:1.17.0

ARG PYTHON_VERSION=3.13.7
FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Системные зависимости для Chromium (Playwright)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget curl \
    libnss3 libxss1 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libglib2.0-0 libgtk-3-0 libpango-1.0-0 libcairo2 \
    libx11-6 libx11-xcb1 libxext6 libxshmfence1 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# deps stage: только зависимости (лучшее кеширование)
FROM base AS deps
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY requirements.txt .
RUN pip install -r requirements.txt
RUN python -m playwright install chromium

# runtime stage: минимальный слой с кодом
FROM base AS runtime
# Переносим питон-зависимости
COPY --from=deps /usr/local /usr/local
# Переносим браузеры Playwright из того же пути
COPY --from=deps /ms-playwright /ms-playwright
# И фиксируем переменную, чтобы рантайм смотрел туда же
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Копирование проекта
COPY . /app

# безопасный пользователь
RUN useradd -ms /bin/bash appuser && chown -R appuser:appuser /app /ms-playwright /usr/local
USER appuser

# Целевые стадии под compose target'ы
FROM runtime AS api
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]

FROM runtime AS worker
CMD ["celery","-A","worker.tasks","worker","--loglevel=INFO"]

FROM runtime AS beat
CMD ["celery","-A","worker.tasks","beat","--loglevel=INFO"]
