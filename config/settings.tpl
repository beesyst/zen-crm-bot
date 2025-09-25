clear_logs: true   # true/false

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
  fingerprint_injector: "2.1.72"
  fingerprint_generator: "2.1.72"

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
      main:
        phone:    null
        email:    null
        website:  195000
        document: 1113191
        address:  195002
        twitter:  1113199
        discord:  1113985
        linkedin: 1113201
        telegram: 1113197
        github:   1114007
        reddit:   1113195
        youtube:  1113193
        medium:   1113983
        fund:     1113987

      contact:
        phone:     null
        email:     null
        twitter:   1110003
        telegram:  1110007
        discord:   1110009
        linkedin:  1110010
        website:   1110011
        forms:     1110012
        position:  1110013

      # project:
      #  docs: 1113191
      #  site: 111001
      #  info: 111003
      #  sent_email_dt: 111007
      #  sent_ticket_dt: 111009
      #  tier: 111010
      #  funding:    201494

    safe_mode:
      dry_run: true           # только заметка
      no_overwrite: true      # не затирать уже заполненные поля

modes:
  research_and_intake:        # Режим 1
    enabled: false            # true/false
    tag_create: ["bot","new"]
    limit: 100                
    rate_limit_sec: 0.2       # троттлинг между сайтами
  enrich_existing:            # Режим 2
    enabled: true             # true/false
    tag_id: [157965]        
    tag_process: ["new"]      # fallback для резолва ID
    page_size: 250            # размер страницы API Kommo
    limit: 5                  # глобальный лимит компаний за прогон
    rate_limit_sec: 0.1       # пауза между компаниями

parser:
  http:
    strategy: "round_robin"  # round_robin/random | "single"
    ua:
      - "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
      - "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
      - "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
      - "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
  nitter:
    enabled: true            # true/false
    strategy: "random"       # random/round_robin
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
    timeout: 15              # таймаут (в сек)
    bad_ttl: 600             # на сколько сек баним инстанс после неудачи
    max_ins: 4               # сколько инстансов за один прогон

socials:
  keys:
    - website
    - document
    - twitter
    - discord
    - telegram
    - youtube
    - linkedin
    - reddit
    - medium
    - github
    - twitter_all
  host_map:
    x.com: twitter
    twitter.com: twitter
    discord.gg: discord
    discord.com: discord
    t.me: telegram
    telegram.me: telegram
    youtube.com: youtube
    youtu.be: youtube
    linkedin.com: linkedin
    lnkd.in: linkedin
    reddit.com: reddit
    medium.com: medium
    github.com: github

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