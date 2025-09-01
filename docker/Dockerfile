# syntax=docker/dockerfile:1.17.0

ARG PYTHON_VERSION=3.13.7
FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Устойчивый apt с повторами и очисткой кешей + nodejs/npm + либы для Chromium
RUN set -eux; \
  echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries; \
  apt-get update; \
  apt-get install -y --no-install-recommends \
    ca-certificates wget curl \
    # nodejs/npm нужны для парсеров (X/Nitter и пр.)
    nodejs npm \
    # зависимости для Chromium/Playwright
    libnss3 libxss1 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libglib2.0-0 libgtk-3-0 libpango-1.0-0 libcairo2 \
    libx11-6 libx11-xcb1 libxext6 libxshmfence1 \
  || ( \
    echo "APT failed once, cleaning caches and retrying..." >&2; \
    apt-get clean; rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates wget curl \
      nodejs npm \
      libnss3 libxss1 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
      libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
      libgbm1 libglib2.0-0 libgtk-3-0 libpango-1.0-0 libcairo2 \
      libx11-6 libx11-xcb1 libxext6 libxshmfence1 \
  ); \
  apt-get clean; \
  rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb

WORKDIR /app

# deps: Python + Playwright + Node deps (лучшее кеширование)
FROM base AS deps
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Python зависимости
COPY requirements.txt .
RUN pip install -r requirements.txt
RUN python -m playwright install chromium

# Node зависимости для локальных скриптов (core/node)
WORKDIR /app/core/node
COPY core/node/package.json core/node/package-lock.json* ./
RUN if [ -f package-lock.json ]; then \
      npm ci --omit=dev --no-audit --no-fund; \
    else \
      npm install --omit=dev --no-audit --no-fund; \
    fi

# runtime: минимальный слой с кодом
FROM base AS runtime
# Перенос Python deps и Playwright браузеры
COPY --from=deps /usr/local /usr/local
COPY --from=deps /ms-playwright /ms-playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Проектный код
WORKDIR /app
COPY . /app

# Перенос node_modules локальных скриптов
COPY --from=deps /app/core/node/node_modules /app/core/node/node_modules
# Важно: чтобы скрипты могли находить модули
ENV NODE_PATH=/app/core/node/node_modules

# Безопасный пользователь
RUN useradd -ms /bin/bash appuser && \
    chown -R appuser:appuser /app /ms-playwright /usr/local
USER appuser

# Целевые стадии под compose target'ы
FROM runtime AS api
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]

FROM runtime AS worker
CMD ["celery","-A","worker.tasks","worker","--loglevel=INFO"]

FROM runtime AS beat
CMD ["celery","-A","worker.tasks","beat","--loglevel=INFO"]
