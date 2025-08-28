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
│   │   └── crm/
│   │       └── kommo.py             # Тонкий клиент Kommo API v4
│   ├── routes/                      # HTTP-маршруты API
│   │   ├── admin.py                 # Админ-ручки (seed, add\_note, service endpoints)
│   │   └── webhooks.py              # Вебхуки от Kommo (bootstrap, события CRM)
│   ├── templates/                   # Jinja/HTML шаблоны
│   │   └── email\_outreach.html     # Шаблон писем для email-канала
│   └── main.py                      # Точка входа FastAPI (инициализация, роуты)
│
├── config/                          # Конфигурация системы
│   ├── settings.yml                 # Главный конфиг (infra, crm, mail, channels, outreach)
│   ├── sites.yml                    # Список сайтов для seed-компаний
│   └── start.py                     # Скрипт начальной настройки окружения
│
├── core/                            # Базовые утилиты и bootstrap
│   ├── bootstrap/
│   │   └── env\_setup.py            # Подготовка окружения и переменных
│   ├── install.py                   # Автоустановка зависимостей
│   ├── log\_setup.py                # Централизованное логирование
│   ├── paths.py                     # Пути и директории проекта
│   ├── settings.py                  # Загрузчик и валидатор конфигурации
│   └── templates/                   # Шаблоны для генерации конфигов и env
│       ├── env.example.tpl
│       ├── .env.stub.tpl
│       ├── settings.example.yml
│       └── settings.yml.tpl
│
├── db/                              # Заглушка под миграции и SQL (если потребуется)
│
├── docker/                          # Docker-инфраструктура
│   ├── docker-compose.yml           # Основной docker-compose (API, worker, db, redis)
│   ├── docker-compose.override.yml
│   └── Dockerfile                   # Сборка образа приложения
│
├── domain/                          # Бизнес-логика (services layer)
│   └── services/
│       ├── companies.py             # Работа с компаниями (создание, enrichment)
│       ├── company_x.py             # Работа с X
│       ├── dedupe.py                # Дедупликация контактов и компаний
│       ├── dispatch.py              # Отправка сообщений в каналы
│       ├── ingest.py                # Парсинг сайтов и сбор информации
│       ├── intake.py                # Bootstrap лида: enrichment + перевод стадий
│       ├── plan.py                  # Построение плана аутрича (order каналов)
│       └── seed.py                  # Seed-компании из config/sites.yml
│
├── infra/                           # Интеграции низкого уровня
│   ├── senders/
│   │   └── email.py                 # Отправка писем (SMTP)
│   └── templating/
│       └── jinja.py                 # Рендеринг HTML-писем через Jinja2
│
├── logs/                            # Логи всех компонентов
│   ├── api.log                      # API FastAPI
│   ├── worker.log                   # Celery worker
│   ├── beat.log                     # Celery beat
│   ├── email.log                    # Отправка писем
│   ├── discord.log                  # Outreach через Discord
│   ├── telegram.log                 # Outreach через Telegram
│   ├── docker.log                   # Docker-скрипты
│   ├── host.log                     # Общий хостовой лог
│   └── zen-crm.log                  # Главный лог сервиса
│
├── modules/                         # Модульная система плагинов аутрича
│   ├── base.py                      # Базовый класс плагинов
│   ├── registry.py                  # Реестр модулей
│   └── outreach/                    # Каналы коммуникации
│       ├── discord.py
│       ├── forms.py
│       └── telegram.py
│
├── storage/                         # Временные и постоянные данные
│   ├── celery/                      # Celery scheduler state
│   │   ├── celerybeat-schedule
│   │   └── ...
│   └── seed/
│       └── state.json               # Индекс текущего seed-компании
│
├── worker/                          # Фоновые задачи
│   └── tasks.py                     # Celery-таски (seed\_next\_company, kickoff\_outreach и др.)
│
├── .env                             # Локальные переменные окружения
├── .env.example                     # Шаблон для .env
├── .gitignore                       # Git-игнор
├── .dockerignore                    # Docker-игнор
├── requirements.txt                 # Python зависимости
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

```mermaid
flowchart TD
    A[sites.yml / внешние источники] --> B[Seed компаний]
    B -->|создание + теги bot,new| C[Kommo CRM компании]
    C --> D[Режим обогащения]
    D -->|парсинг, соцсети, контакты| E[Обновление кастомных полей]
    E --> F[Bootstrap лида → READY_FOR_OUTREACH]
    F --> G[План аутрича (email, Discord, TG...)]
    G --> H[Рассылка сообщений]
    H --> I[Финализация: заметки + стадии]
 
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







