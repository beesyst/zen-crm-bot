# syntax=docker/dockerfile:1.17.0

ARG PYTHON_VERSION
ARG PYTHON_DEBIAN
FROM python:${PYTHON_VERSION}-${PYTHON_DEBIAN} AS base

ARG NODE_VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_VERSION=${NODE_VERSION}

# Базовые пакеты + системные либы для Chromium/Playwright (без node из apt)
RUN set -eux; \
  echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries; \
  apt-get update; \
  apt-get install -y --no-install-recommends \
    ca-certificates wget curl xz-utils \
    libnss3 libxss1 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libglib2.0-0 libgtk-3-0 libpango-1.0-0 libcairo2 \
    libx11-6 libx11-xcb1 libxext6 libxshmfence1; \
  apt-get clean; \
  rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb

# Установка Node из официального tarball по NODE_VERSION (linux-x64 / arm64)
RUN set -eux; \
  arch="$(dpkg --print-architecture)"; \
  case "$arch" in \
    amd64) node_arch="x64" ;; \
    arm64) node_arch="arm64" ;; \
    *) echo "Unsupported arch: $arch" >&2; exit 1 ;; \
  esac; \
  cd /usr/local; \
  curl -fsSLo node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
  tar -xJf node.tar.xz; \
  rm -f node.tar.xz; \
  mv "node-v${NODE_VERSION}-linux-${node_arch}" node; \
  ln -sf /usr/local/node/bin/node /usr/local/bin/node; \
  ln -sf /usr/local/node/bin/npm  /usr/local/bin/npm; \
  ln -sf /usr/local/node/bin/npx  /usr/local/bin/npx; \
  node -v && npm -v

ENV PATH="/usr/local/node/bin:${PATH}"

WORKDIR /app

# deps: только Python
FROM base AS deps
COPY requirements.txt .
RUN pip install -r requirements.txt

# deps-npm: node_modules + playwright (кэшируемый слой)
FROM base AS deps-npm
WORKDIR /app/core/node
# Копируем только package.json (+ lock если есть) — это ключ к стабильному кэшу npm
COPY core/node/package.json core/node/package-lock.json* ./
# Если есть package-lock.json → npm ci, иначе → npm install
RUN if [ -f package-lock.json ]; then \
      npm ci --omit=dev --no-audit --no-fund; \
    else \
      npm install --omit=dev --no-audit --no-fund; \
    fi

# Установка браузеров (кэш вместе с node_modules)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN mkdir -p /ms-playwright && \
    ./node_modules/.bin/playwright --version && \
    ./node_modules/.bin/playwright install chromium

# runtime: финальный образ
FROM base AS runtime

# Python deps
COPY --from=deps /usr/local /usr/local

# Node deps и браузеры из кэшируемого слоя
COPY --from=deps-npm /app/core/node /app/core/node
COPY --from=deps-npm /ms-playwright /ms-playwright

# Проектный код - копируем после deps, чтобы правки кода не ломали кэш npm/playwright
WORKDIR /app
COPY . /app

# Настройка PATH/ENV
ENV NODE_PATH=/app/core/node/node_modules
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PATH="/app/core/node/node_modules/.bin:/usr/local/node/bin:${PATH}"

# Шорткат на бинарь playwright
RUN ln -sf /app/core/node/node_modules/.bin/playwright /usr/local/bin/playwright

# Профиль login-shell, чтобы PATH/переменные не терялись
RUN printf '%s\n' \
  'export NODE_PATH=/app/core/node/node_modules' \
  'export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright' \
  'export PATH="/app/core/node/node_modules/.bin:/usr/local/node/bin:$PATH"' \
  > /etc/profile.d/10-app-path.sh && chmod +x /etc/profile.d/10-app-path.sh

# Пользователь и права
RUN useradd -ms /bin/bash appuser && \
    chown -R appuser:appuser /app /ms-playwright /usr/local /usr/local/node || true
USER appuser

# alias targets
FROM runtime AS api
FROM runtime AS worker
FROM runtime AS beat
FROM runtime AS job
