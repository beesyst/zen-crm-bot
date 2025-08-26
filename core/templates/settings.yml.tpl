app:
  host: "0.0.0.0"
  port: 8000

infra:
  redis_url: "redis://redis:6379/0"
  database_url: "postgresql+psycopg2://kommo:kommo@db:5432/kommo"

crm:
  provider: "kommo"
  kommo:
    base_url: "https://YOUR-SUBDOMAIN.kommo.com"
    access_token: "PASTE_TOKEN"
