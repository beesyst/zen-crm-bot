clear_logs: {{ clear_logs | default(true) }}

images:
  postgres: "{{ images.postgres | default('postgres:17.6') }}"
  redis: "{{ images.redis | default('redis:8.2-alpine') }}"

app:
  host: "{{ app.host | default('0.0.0.0') }}"
  port: {{ app.port | default(8000) }}

infra:
  redis_url: "{{ infra.redis_url | default('redis://redis:6379/0') }}"
  database_url: "{{ infra.database_url | default('postgresql+psycopg2://kommo:kommo@db:5432/kommo') }}"

crm:
  provider: "{{ crm.provider | default('kommo') }}"
  kommo:
    base_url: "{{ crm.kommo.base_url | default('https://YOUR-SUBDOMAIN.kommo.com/') }}"
    access_token: "PASTE_TOKEN"       # секрет редактируется
    secret_key: "{{ crm.kommo.secret_key | default('REDACTED') }}"
    integration_id: "{{ crm.kommo.integration_id | default('REDACTED') }}"
    stages_map: "{{ crm.kommo.stages_map | default('config/stages.map.json') }}"
    # Произвольные поля и режимы — тянем как есть, если юзер их добавил в settings.yml.
    fields: {{ crm.kommo.fields | default({}) }}
    safe_mode:
      dry_run: {{ crm.kommo.safe_mode.dry_run | default(false) }}
      no_overwrite: {{ crm.kommo.safe_mode.no_overwrite | default(true) }}

modes:
  research_and_intake:
    enabled: {{ modes.research_and_intake.enabled | default(false) }}
    tag_create: {{ modes.research_and_intake.tag_create | default(['bot','new']) }}
  enrich_existing:
    enabled: {{ modes.enrich_existing.enabled | default(true) }}
    tag_process: {{ modes.enrich_existing.tag_process | default(['new']) }}

link_collections: {{ link_collections | default([]) }}

mail:
  smtp_host: "{{ mail.smtp_host | default('smtp.yourhost.com') }}"
  smtp_port: {{ mail.smtp_port | default(465) }}
  smtp_user: "{{ mail.smtp_user | default('mailer@yourhost.com') }}"
  smtp_pass: "secret"                 # секрет редактируется
  from_name: "{{ mail.from_name | default('Noders Outreach') }}"
  from_email: "{{ mail.from_email | default('mailer@yourhost.com') }}"

channels:
  discord:
    webhook_fallback: "{{ channels.discord.webhook_fallback | default('') }}"
  telegram:
    bot_token: "xxx"                  # секрет редактируется

outreach:
  order: {{ outreach.order | default(['email','discord','form','telegram']) }}
  quiet_hours:
    start: "{{ outreach.quiet_hours.start | default('21:00') }}"
    end: "{{ outreach.quiet_hours.end | default('08:00') }}"
    timezone: "{{ outreach.quiet_hours.timezone | default('Europe/Moscow') }}"
