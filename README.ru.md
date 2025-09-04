# Zen-CRM-Bot (ZCB)

**Zen-CRM-Bot** — модульная автоматизированная система для работы с лидами и компаниями в CRM. Объединяет парсинг сайтов, автоматическое обогащение контактов, построение плана аутрича и отправку сообщений по каналам (email, формы, Discord, Telegram). Интегрируется с Kommo CRM для централизованного управления лидами и компаниями.

## Основные возможности

* **Автоматическое создание компаний** — по списку сайтов (`config/sites.yml`) с сохранением домена в поле Web (company).
* **Обогащение данных** — парсинг сайта, поиск email/форм/соцсетей, автоматическое заполнение кастомных полей.
* **Гибкий план аутрича** — генерация плана по каналам (email, Discord, формы, Telegram).
* **Цепочка задач в Celery** — ingest → dedupe → plan → send → finalize.
* **Поддержка компаний и лидов** — обновление стадий, добавление заметок и тегов.
* **Интеграция с Kommo** — тонкий клиент для работы с лидами и компаниями через v4 API.
* **Логирование и аудит** — подробные логи пайплайна, события пишутся в Kommo.
* **Периодическая обработка** — celery-beat выполняет `seed_next_company` каждые 60 секунд.

## Где можно использовать

* **Автоматизация аутрича для крипто- и IT-проектов**
* **Постоянный мониторинг новых сайтов/проектов**
* **Автоматическая сегментация и enrichment компаний**
* **Упрощение работы менеджеров в Kommo**

## Технологический стек

* **Python 3.12+**
* **FastAPI** — REST API (admin/webhooks).
* **Celery + Redis** — фоновые задачи и планировщик.
* **PostgreSQL** — хранилище данных.
* **Kommo API v4** — CRM интеграция.
* **YAML/JSON** — конфигурация и хранение состояния.

### Управление стеком

Сервис управляется через `start.sh`:

```bash
./start.sh dev            # собрать и запустить стек в форграунде
./start.sh dev-bg         # собрать и запустить стек в фоне (detached)
./start.sh stop           # остановить локальный стек
./start.sh logs           # смотреть логи всех сервисов
./start.sh health         # проверить API (http://localhost:8000/health)
./start.sh test-webhook   # отправить тестовый вебхук Kommo
./start.sh prod-up        # собрать и запустить стек на сервере (docker compose -d)
./start.sh prod-down      # остановить стек на сервере
./start.sh run-research   # одноразовый запуск режима 1 (research)
./start.sh run-enrich     # одноразовый запуск режима 2 (enrich)
```

## Архитектура

### Компоненты системы

1. **API (`app/main.py`)** — точка входа, маршруты `/admin`, `/webhooks`, healthcheck.
2. **Адаптеры CRM (`app/adapters/crm/kommo.py`)** — тонкий клиент Kommo.
3. **Celery-задачи (`worker/tasks.py`)** — пайплайны аутрича и seed-компаний.
4. **Domain-сервисы (`domain/services/*`)** — логика парсинга, enrichment, dedupe, seed.
5. **Конфигурация (`config/*.yml`)** — настройки подключения, список сайтов, стадии CRM.
6. **Логирование (`logs/`)** — централизованные логи API, worker, beat.

### Структура проекта

```
zen-crm-bot/
├── app/                             # Веб-приложение (FastAPI)
│   ├── adapters/                    # Адаптеры для внешних систем
│       └── crm/
│           └── kommo.py             # Тонкий клиент Kommo API v4
│   ├── routes/                      # HTTP-маршруты API
│       ├── admin.py                 # Служебные ручки (seed, notes, сервисные эндпоинты)
│       └── webhooks.py              # Вебхуки Kommo (bootstrap лида и события CRM)
│   ├── templates/                   # Jinja/HTML шаблоны
│       └── email_outreach.html      # Шаблон писем для email-канала
│   └── main.py                      # Инициализация FastAPI, DI, роуты
│
├── cli/                             # Одноразовые CLI-пайплайны (внутри docker job)
│   ├── enrich.py                    # "Enrich Existing": обогащение компаний по тегам
│   └── research.py                  # "Research & Intake": сидинг компаний из sites.yml
│
├── config/                          # Конфигурация системы
│   ├── settings.yml                 # Главный конфиг (infra, crm, mail, channels, outreach)
│   ├── sites.yml                    # Список сайтов для seed-компаний
│   └── start.py                     # Скрипт начальной настройки окружения
│
├── core/                            # Базовая инфраструктура и утилиты
│   ├── bootstrap/
│       └── env_setup.py             # Подготовка окружения и переменных
│   ├── node/
│       └── package.json    
│   ├── parser/                      # Парсеры: веб, YouTube, X (Nitter/скрейпер), и т.д.
│       ├── browser_fetch.js         # Вызовы Playwright из node (headless fetch)
│       ├── link_aggregator.py       # Слияние/чистка ссылок, эвристики
│       ├── twitter_scraper.js       # JS-скрейпер X (fallback)
│       ├── twitter.py               # Добыча ссылок/метаданных из X (через Nitter/скрейпер)
│       ├── web.py                   # Парсер сайтов (главная/документация/соц-иконки)
│       └── youtube.py               # Извлечение каналов/ссылок YouTube         
│   ├── templates/                   # Шаблоны для генерации примеров конфигов и env
│       ├── .env.stub.tpl
│       ├── main_template.json
│       ├── settings.example.yml
│       └── settings.example.yml.tpl
│   ├── collector.py                 # Сбор соц-ссылок/метаданных (агрегация результатов парсеров)
│   ├── console.py                   # Единый формат терминальных меток: [ok]/[skip]/[add]/...
│   ├── install.py                   # Автоустановка зависимостей
│   ├── log_setup.py                 # Централизованное логирование
│   ├── normalize.py                 # Нормализация брендов/доменов (для логов и storage)
│   ├── orchestrator.py              # Оркестратор пайплайнов research/enrich (консоль+файловый лог)
│   ├── paths.py                     # Все пути проекта (storage/logs/config и т.п.)
│   └── settings.py                  # Чтение settings.yml и вспомогательные флаги/образы
│
├── db/                              # Заглушка под миграции и SQL (если потребуется)
│
├── docker/                          # Docker-инфраструктура
│   ├── docker-compose.yml           # Основной стек: api/worker/beat/db/redis/job
│   └── Dockerfile                   # Образ приложения (многостейдж)
│
├── domain/                          # Бизнес-логика (services layer)
│   └── services/
│       ├── companies.py             # Работа с компаниями (создание, enrichment)
│       ├── company_x.py             # Работа с X
│       ├── dedupe.py                # Дедупликация контактов и компаний
│       ├── dispatch.py              # Отправка сообщений в каналы
│       ├── enrich.py                # Алгоритм обогащения компании по URL
│       ├── ingest.py                # Парсинг сайтов и сбор информации
│       ├── intake.py                # Bootstrap лида: enrichment + перевод стадий
│       ├── plan.py                  # Построение плана аутрича (order каналов)
│       └── seed.py                  # Seed-компании из config/sites.yml
│
├── infra/                           # Интеграции низкого уровня
│   ├── senders/
│       └── email.py                 # Отправка писем (SMTP)
│   └── templating/
│       └── jinja.py                 # Рендеринг HTML-писем через Jinja2
│
├── logs/                            # Логи (files rolling)
│   ├── host.log                     # Общий лог (оркестратор, CLI-режимы и др.)
│   ├── kommo.log                    # Трафик/события Kommo-адаптера
│   └── setup.log                    # Подготовка окружения (start.py: проверка, build, up)
│
├── modules/                         # Плагины каналов аутрича
│   ├── outreach/
│   │   ├── discord.py               # Отправка в Discord (webhook)
│   │   ├── forms.py                 # Заполнение контакт-форм
│   │   └── telegram.py              # Отправка в Telegram
│   ├── base.py                      # Базовый интерфейс плагина канала
│   └── registry.py                  # Реестр и фабрика плагинов
│
├── storage/                         # Временные и постоянные данные
│   ├── celery/                      # Celery scheduler state
│       ├── celerybeat-schedule
│       └── ...
│   ├── projects/                    # Кешированные профили проектов (по доменам)
        └── ...
│   └── seed/
│       └── state.json               # Индекс текущего seed-компании
│
├── worker/                          # Фоновые задачи
│   └── tasks.py                     # Celery-таски (seed\_next\_company, kickoff\_outreach и др.)
│
├── .env                             # Локальные переменные окружения
├── .env.example                     # Шаблон для .env
├── README.md                        # Документация (EN)
├── README.ru.md                     # Документация (RU)
├── requirements.txt                 # Python зависимости
└── start.sh                         # Главный скрипт запуска (API, worker, beat)
```


## Pipeline: Как это работает?

### Режимы работы

1. **Режим ресерча `research_and_intake`**
   * Берет сайты из `config/sites.yml` или с внешних источников.
   * Сохраняет сайт в поле **Web**.
   * Добавляет теги **bot** и **new**.
   * Эти компании ждут последующего обогащения.

2. **Режим обогащения `enrich_existing`**
   * Работает только с компаниями, где стоит тег **new**.
   * Парсит сайт компании и обогащает карточку:
     * Docs, LinkedIn, Telegram, Discord, X (Twitter), Email и др.
   * Обновляет кастомные поля.
   * Добавляет заметку в Kommo о выполненной операции.
   * После обогащения компания может быть передана в аутрич.

### Общий пайплайн

1. **Seed компаний**
   * Задача Celery `seed_next_company` берет URL из `config/sites.yml`.
   * Создает компанию в Kommo (`<name>`, теги `bot`, `new`).
   * Записывает сайт в поле **Web**.

2. **Bootstrap лида**
   * Вебхук Kommo вызывает `bootstrap_new_lead`.
   * Парсится сайт, заполняются поля (Docs, LinkedIn, Telegram и др.).
   * Лид переводится в стадию `READY_FOR_OUTREACH`.

3. **Аутрич**
   * Задача `kickoff_outreach` запускает цепочку:
     * `t_ingest` → `t_dedupe` → `t_plan` → `t_dispatch_and_finalize`.
   * Генерируется план рассылки по каналам (email, Discord, формы, Telegram).
   * Сообщения отправляются автоматически.
   * В Kommo добавляется заметка, стадия обновляется.

4. **Финализация**
   * Успешные каналы фиксируются в заметке Kommo.
   * Ошибки логируются и также отображаются в CRM.

### Диаграмма пайплайна

```flowchart TD
    A[sites.yml / внешние источники] --> B[Seed компаний]
    B -->|создание + теги bot,new| C[Kommo CRM компании]
    C --> D[Режим обогащения]
    D -->|парсинг, соцсети, контакты| E[Обновление кастомных полей]
    E --> F[Bootstrap лида → READY_FOR_OUTREACH]
    F --> G[План аутрича: email, Discord, Telegram]
    G --> H[Рассылка сообщений]
    H --> I[Финализация: заметки + стадии]
```
 
## Установка и запуск

```bash
git clone https://github.com/beesyst/zen-crm-bot.git
cd zen-crm-bot
bash start.sh
```

## Настройка конфигурации

### Основное `config/settings.yml`

| Параметр                 | Описание                                       |
| ------------------------ | ---------------------------------------------- |
| `infra.redis_url`        | Подключение к Redis (broker + backend Celery). |
| `infra.database_url`     | Подключение к PostgreSQL.                      |
| `crm.kommo.base_url`     | Базовый URL Kommo.                             |
| `crm.kommo.access_token` | Токен интеграции Kommo API.                    |
| `crm.kommo.fields.web`   | ID кастомного поля для сайта у компаний.       |
| `crm.kommo.stages_map`   | JSON с маппингом стадий (код → ID).            |
| `mail`                   | Настройки SMTP для email-канала.               |
| `channels`               | Конфиг каналов (Discord, Telegram).            |
| `outreach.order`         | Приоритет каналов аутрича.                     |

## Роли контейнеров

Все сервисы используют общий образ `zencrm-app` и одни и те же переменные окружения. Логи и рабочие данные монтируются в `./logs` и `./storage`.

- **api** — веб-приложение (FastAPI, HTTP API)
  - Отвечает на запросы: `/health`, `/admin/*`, `/webhooks/*`.
  - Работает с PostgreSQL и Redis, ставит фоновые задания в очередь Celery.
  - Запускается через `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
  - Проверка: `curl -fsS http://localhost:8000/health`.

- **worker** — исполнитель фоновых задач (Celery worker)
  - Обрабатывает пайплайны `ingest → dedupe → plan → dispatch → finalize`.
  - Выполняет парсинг сайтов (в т.ч. через Playwright), enrichment карточек и вспомогательные задачи.
  - Запускается как `celery -A worker.tasks worker --loglevel=INFO`.

- **beat** — планировщик периодических задач (Celery beat)
  - По расписанию кидает задачи воркеру (например, `seed_next_company` каждые 60 сек).
  - Состояние расписания хранит в `storage/celery/celerybeat-schedule` (папка примонтирована).
  - Запускается как  
    `celery -A worker.tasks beat --loglevel=INFO --schedule /app/storage/celery/celerybeat-schedule`.

- **job** — одноразовый рабочий контейнер для ручных запусков
  - Используется для утилит и разовых пайплайнов, наследует окружение приложения.
  - Примеры:
    - `docker compose run --rm job python -m cli.research`
    - `docker compose run --rm job python -m cli.enrich`
  - Не имеет healthcheck/автоперезапуска, завершается по окончании команды.






