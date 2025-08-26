# Application
APP_HOST=0.0.0.0
APP_PORT=8000

# Infra
REDIS_URL=redis://redis:6379/0
DATABASE_URL=postgresql+psycopg2://kommo:kommo@db:5432/kommo

# CRM
CRM_PROVIDER=kommo
KOMMO_BASE_URL=https://YOUR-SUBDOMAIN.kommo.com
KOMMO_ACCESS_TOKEN=PASTE_TOKEN

# Email
SMTP_HOST=smtp.yourhost.com
SMTP_PORT=465
SMTP_USER=mailer@yourhost.com
SMTP_PASS=secret
SMTP_FROM="Noders Outreach <mailer@yourhost.com>"

# Bots/Channels
DISCORD_BOT_TOKEN=xxx
TELEGRAM_BOT_TOKEN=xxx
