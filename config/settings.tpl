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
      web:       195000
      docs:      1113191
      address:   195002
      x:         1113199
      discord:   1113985
      linkedin:  1113201
      telegram:  1113197
      github:    1114007
      reddit:    1113195
      youtube:   1113193
      medium:    1113983
      fund:      1113987
      # Project
      #  docs: 1113191
      #  site: 111001
      #  info: 111003
      #  sent_email_dt: 111007
      #  sent_ticket_dt: 111009
      #  tier: 111010
      #  funding:    201494

    safe_mode:
      dry_run: true          # только заметка
      no_overwrite: true     # не затирать уже заполненные поля

modes:
  research_and_intake:        # Режим 1
    enabled: false
    tag_create: ["bot","new"]
    limit: 100                
    rate_limit_sec: 0.2       # троттлинг между сайтами
  enrich_existing:            # Режим 2
    enabled: true
    tag_id: [157965]        
    tag_process: ["new"]      # fallback для резолва ID
    page_size: 250            # размер страницы API Kommo
    limit: 5                  # глобальный лимит компаний за прогон
    rate_limit_sec: 0.1       # пауза между компаниями

parser:
  nitter:
    enabled: true
    instances: [
      "https://nitter.net",
      "https://xcancel.com",
      "https://nuku.trabun.org",
      "https://nitter.tiekoetter.com",
      "https://nitter.space",
      "https://lightbrd.com",
      "https://nitter.privacyredirect.com",
      "https://nitter.kuuro.net"
    ]
    retry_per_instance: 2
    timeout_sec: 14
    bad_ttl_sec: 600
    use_stealth: true
    max_instances_try: 8

socials:
  keys:                # порядок важен
    - websiteURL
    - documentURL
    - twitterURL
    - discordURL
    - telegramURL
    - youtubeURL
    - linkedinURL
    - redditURL
    - mediumURL
    - githubURL
    - twitterAll       # коллекция
  social_hosts:        # используется для фильтра websiteURL в агрегаторах
    - x.com
    - twitter.com
    - discord.gg
    - discord.com
    - t.me
    - telegram.me
    - youtube.com
    - youtu.be
    - linkedin.com
    - lnkd.in
    - reddit.com
    - medium.com
    - github.com

link_collections:
  - linktr.ee
  - link3.to
  - bento.me
  - hub.xyz

contacts:
  roles:
    support:   ["support","tech support","customer support","cs","helpdesk","help","customer","service","contact","admin"]
    contact:   ["contact","contacts","get in touch","reach us"]
    sales:     ["sales","bizdev","business","partnerships"]
    partners:  ["partners","partnership"]
    bizdev:    ["bizdev","bd","bdm"]
    marketing: ["marketing","growth"]
    devrel:    ["devrel","developer relations","developer-relations"]
    community: ["community","cm","community manager","community-management"]

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