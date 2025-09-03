clear_logs: true   # или false

images:
  postgres: "postgres:17.6"
  redis: "redis:8.2-alpine"

runtime:
  python_version: "3.13.7"
  python_debian: "slim-bookworm"
  node_version: "24.7.0"

node_deps:
  engines_node: ">=24.7.0 <25"
  playwright: "1.55.0"
  fingerprint_injector: "2.1.70"

app:
  host: "0.0.0.0"
  port: 8000

infra:
  redis_url: "redis://redis:6379/0"
  database_url: "postgresql+psycopg2://kommo:kommo@db:5432/kommo"

crm:
  provider: "kommo"
  kommo:
    base_url: ""
    access_token: ""
    secret_key: ""
    integration_id: ""
    stages_map: "config/stages.map.json"

    fields:
      # Main
      phone:     null
      email:     null
      address:   195002
      web:       787593
      x:         954860      
      linkedin:  1047984
      telegram:  1047992
      reddit:    1047994
      medium:    1315150
      youtube:   1048044
      docs:      1048046
      # Project
      #  docs: 1113191
      #  site: 111001
      #  info: 111003
      #  sent_email_dt: 111007
      #  sent_ticket_dt: 111009
      #  tier: 111010
      #  funding: 111011

    safe_mode:
      dry_run: false         # только заметка
      no_overwrite: true     # не затирать уже заполненные поля

modes:
  research_and_intake:
    enabled: false
    tag_create: ["bot","new"]     # режим 1: создаем с этими тегами
  enrich_existing:
    enabled: true
    tag_process: ["new"]          # режим 2: работаем только если есть это тег

link_collections:
  - linktr.ee
  - link3.to
  - bio.link
  - beacons.ai
  - carrd.co
  - withkoji.com
  - taplink.cc
  - linkin.bio
  - t.co


mail:
  smtp_host: "smtp.yourhost.com"
  smtp_port: 465
  smtp_user: "mailer@yourhost.com"
  smtp_pass: "secret"
  from_name: "Noders Outreach"
  from_email: "mailer@yourhost.com"

channels:
  discord:
    webhook_fallback: ""
  telegram:
    bot_token: "123456:ABCDEF"

outreach:
  order: ["email","discord","form","telegram"]
  quiet_hours:
    start: "21:00"
    end: "08:00"
    timezone: "Europe/Moscow"