const { chromium } = require('playwright');
const { newInjectedContext } = require('fingerprint-injector');

// Разрешенные режимы ожидания
const WAIT_STATES = new Set(['load', 'domcontentloaded', 'networkidle', 'commit', 'nowait']);

// Парсинг аргументов CLI
function parseArgs(argv) {
  const args = {};
  let positionalUrl = null;

  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];

    if (a === '--html') args.html = true;
    else if (a === '--text') args.text = true;
    else if (a === '--socials') args.socials = true;
    else if (a === '--raw') { args.html = true; args.text = true; args.raw = true; }
    else if (a === '--js') { args.js = String(argv[++i]).toLowerCase() !== 'false'; }
    else if (a === '--url') args.url = argv[++i];
    else if (a === '--wait') args.wait = argv[++i];
    else if (a === '--timeout') args.timeout = Number(argv[++i]);
    else if (a === '--ua') args.ua = argv[++i];
    else if (a === '--screenshot') args.screenshot = argv[++i];
    else if (a === '--headers') {
      try { args.headers = JSON.parse(argv[++i]); } catch { args.headers = {}; }
    } else if (a === '--cookies') {
      try { args.cookies = JSON.parse(argv[++i]); } catch { args.cookies = []; }
    } else if (a === '--retries') {
      args.retries = Math.max(0, Number(argv[++i]) || 0);
    } else if (!a.startsWith('-') && !positionalUrl) {
      // Поддержка вызова: node browser_fetch.js <URL> [--raw]
      positionalUrl = a;
    }
  }

  if (!args.url && positionalUrl) args.url = positionalUrl;
  return args;
}


// Простейший детектор антибот-страниц (Cloudflare/403/503 и т.п.) - для телеметрии
async function detectAntiBot(page, response) {
  try {
    const server = response?.headers()?.server || '';
    const status = typeof response?.status === 'function' ? response.status() : 0;

    if (status === 403 || status === 503) {
      return { detected: true, kind: String(status), server };
    }
    if (server && /cloudflare/i.test(server)) {
      const html = await page.content();
      const low = (html || '').slice(0, 50000).toLowerCase();
      const needles = [
        'verifying you are human',
        'checking your browser',
        'review the security',
        'cf-challenge',
        'cloudflare',
        'attention required!',
      ];
      if (needles.some(n => low.includes(n))) {
        return { detected: true, kind: 'cloudflare', server };
      }
    }
  } catch {}
  return { detected: false, kind: '', server: '' };
}

// Нормализация twitter → x.com
function normalizeTwitter(u) {
  try { return u.replace(/https?:\/\/(www\.)?twitter\.com/i, 'https://x.com'); }
  catch { return u; }
}

// Основная функция: навигация, рендер и сбор данных
async function browserFetch(opts) {
  const {
    url,
    wait = 'domcontentloaded',
    timeout = 30000,
    ua,
    headers = {},
    cookies = [],
    screenshot,
    html = false,
    text = false,
    js = true,
    retries = 1,
  } = opts || {};

  if (!url) throw new Error('url is required');

  const waitUntil = WAIT_STATES.has(wait) ? (wait === 'nowait' ? null : wait) : 'domcontentloaded';

  const launchArgs = [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--disable-blink-features=AutomationControlled',
  ];

  // аккумулируем логи консоли страницы (полезно для диагностики)
  const consoleLogs = [];

  // один попытка-запуск браузера и сбор данных
  const attempt = async () => {
    const startedAt = Date.now();
    const browser = await chromium.launch({ headless: true, args: launchArgs });
    let context;
    try {
      context = await newInjectedContext(browser, {});
    } catch {
      context = await browser.newContext({
        userAgent: ua || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        javaScriptEnabled: js !== false,
        ignoreHTTPSErrors: true,
        bypassCSP: true,
        viewport: { width: 1366, height: 768 },
        extraHTTPHeaders: { ...headers, 'Accept-Language': 'en-US,en;q=0.9' },
      });
    }
    // поддержка --cookies
    if (Array.isArray(cookies) && cookies.length) {
      try { await context.addCookies(cookies); } catch { /* ignore */ }
    }

    let page;
    try {
      page = await context.newPage();
      page.on('console', (msg) => {
        try { consoleLogs.push({ type: msg.type(), text: msg.text() }); } catch {}
      });

      // надежная навигация: несколько вариантов waitUntil + попытка дождаться networkidle
      async function robustGoto(p, targetUrl) {
        const tries = [
          { waitUntil: waitUntil || 'domcontentloaded', timeout },
          { waitUntil: 'load',                           timeout },
          { waitUntil: 'commit',                         timeout },
          { waitUntil: 'networkidle',                    timeout }, // добавили явную попытку
        ];
        for (const opt of tries) {
          try {
            const r = await p.goto(targetUrl, opt);
            try { await p.waitForLoadState('networkidle', { timeout: 12000 }); } catch {}
            return r;
          } catch (e) { /* следующий режим */ }
        }
        return null;
      }

      const resp = await robustGoto(page, url);

      // затем - пауза и скролл
      try { await page.waitForTimeout(800); } catch {}
      try {
        await page.evaluate(async () => {
          const delay = (ms) => new Promise(r => setTimeout(r, ms));
          let last = 0;
          for (let i = 0; i < 8; i++) {
            window.scrollTo(0, document.body.scrollHeight);
            await delay(250);
            const y = window.scrollY;
            if (Math.abs(y - last) < 10) break;
            last = y;
          }
        });
      } catch {}

      // подождать зоны футера/социалок
      try {
        await page.waitForSelector(
          'footer a[href], [role="contentinfo"] a[href], [class*="social"] a[href]',
          { timeout: 2500 }
        );
      } catch {}

      // явно дождаться самих соц-якорей (SPA часто дорисовывает)
      try {
          await page.waitForFunction(() => {
            const q = (s) => document.querySelector(s);
            const hasA =
              q('a[href*="twitter.com"]') || q('a[href*="x.com"]') ||
              q('a[href*="discord.gg"]') || q('a[href*="discord.com"]') ||
              q('a[href*="t.me"]') || q('a[href*="telegram.me"]') ||
              q('a[href*="github.com"]') || q('a[href*="medium.com"]') ||
              q('a[href*="youtube.com"]') || q('a[href*="youtu.be"]') ||
              q('a[href*="linkedin.com"]') || q('a[href*="lnkd.in"]') ||
              q('a[href*="reddit.com"]');
            const hasLd = !!document.querySelector('script[type="application/ld+json"]');
            return hasA || hasLd;
          }, { timeout: 7000 });
      } catch {}


      // опциональный скриншот
      if (screenshot) {
        try { await page.screenshot({ path: screenshot, fullPage: true }); } catch {}
      }

      const finalUrl = page.url();
      // статус - телеметрия; для spa часто 0
      let status = 0;
      try { status = resp ? (typeof resp.status === 'function' ? resp.status() : 0) : 0; } catch {}

      // ok - чтобы не отдавать undefined в json
      const ok = true;

      // сбор соц-ссылок прямо в браузере (абсолютные URL + список всех твиттер-ссылок)
      const socials = await page.evaluate((base) => {
        const rxTwitter = /twitter\.com|x\.com/i;
        const patterns = {
          twitterURL: rxTwitter,
          discordURL: /discord\.gg|discord\.com/i,
          telegramURL: /t\.me|telegram\.me/i,
          youtubeURL: /youtube\.com|youtu\.be/i,
          linkedinURL: /linkedin\.com|lnkd\.in/i,
          redditURL: /reddit\.com/i,
          mediumURL: /medium\.com/i,
          githubURL: /github\.com/i,
        };

        const toAbs = (href) => {
          try {
            if (!href) return '';
            if (href.startsWith('//')) return location.protocol + href;
            return href.startsWith('http') ? href : new URL(href, base).href;
          } catch { return href || ''; }
        };

        const unwrapRedirect = (href) => {
          try {
            const u = new URL(href, base);
            const p = u.pathname || '';
            if (/^\/(out|redirect|external|go|away|r|link|jump)($|[\/?])/i.test(p)) {
              const keys = ['url','u','to','target','redirect','redirect_uri','dest','destination','link'];
              for (const k of keys) {
                const v = u.searchParams.get(k);
                if (v) return v;
              }
            }
            return href;
          } catch { return href; }
        };

        const acc = Object.fromEntries(Object.keys(patterns).map(k => [k, '']));
        const twitterAll = new Set();

        // <a href>
        document.querySelectorAll('a[href]').forEach(a => {
          const raw = (a.getAttribute('href') || '').trim();
          if (!raw || raw === '#' || raw.startsWith('#')) return;
          if (raw.startsWith('javascript:') || raw.startsWith('mailto:') || raw.startsWith('tel:')) return;

          const rel = (a.getAttribute('rel') || '').toLowerCase();
          const aria = (a.getAttribute('aria-label') || '').toLowerCase();
          const title = (a.getAttribute('title') || '').toLowerCase();
          const text  = (a.textContent || '').toLowerCase();

          let href = toAbs(unwrapRedirect(raw));

          // доп. извлечение из data-атрибутов и onclick
          const candAttrs = [
            a.getAttribute('data-href'),
            a.getAttribute('data-url'),
            a.getAttribute('data-target'),
            a.getAttribute('data-link'),
          ].filter(Boolean);

          let onclick = a.getAttribute('onclick') || '';
          if (onclick && /https?:\/\//i.test(onclick)) {
            try {
              const m = onclick.match(/https?:\/\/[^\s"'()]+/ig);
              if (m && m.length) candAttrs.push(...m);
            } catch {}
          }

          for (let c of candAttrs) {
            try {
              c = toAbs(unwrapRedirect(String(c)));
              // если это discord/x - берем его вместо внутреннего /discord
              if (!patterns.discordURL.test(href) && patterns.discordURL.test(c)) href = c;
              if (!rxTwitter.test(href) && rxTwitter.test(c)) href = c;
            } catch {}
          }

          // внутренняя «заглушка» /discord → отдаем как есть (дальше python разрулит редирект)
          if (!patterns.discordURL.test(href) && /(^|\b)discord\b/i.test(raw)) {
            href = toAbs(raw);
          }

          // если по домену Discord не распознан, но текст/aria/title содержат "discord" - считаем это discord-кнопкой
          if (!acc.discordURL && !patterns.discordURL.test(href) &&
              (text.includes('discord') || aria.includes('discord') || title.includes('discord'))) {
            acc.discordURL = href; // пусть Python потом развернёт до discord.com/invite/...
          }

          // обычная доменная проверка
          for (const [key, rx] of Object.entries(patterns)) {
            if (!acc[key] && (rx.test(href) || rx.test(rel) || rx.test(aria) || rx.test(title))) {
              acc[key] = href;
            }
          }
          if (rxTwitter.test(href)) twitterAll.add(href);
        });


        // JSON-LD sameAs
        try {
          const scripts = Array.from(document.querySelectorAll('script[type="application/ld+json"]'));
          for (const s of scripts) {
            let data = null;
            try { data = JSON.parse(s.textContent || '{}'); } catch {}
            const items = Array.isArray(data) ? data : (data ? [data] : []);
            for (const it of items) {
              const same = it && (it.sameAs || it.sameas || it.SameAs);
              const arr = Array.isArray(same) ? same : (typeof same === 'string' ? [same] : []);
              for (let href of arr) {
                if (typeof href !== 'string') continue;
                href = toAbs(unwrapRedirect(href));
                if (!acc.twitterURL && /twitter\.com|x\.com/i.test(href)) acc.twitterURL = href;
                else if (!acc.discordURL && /discord\.(gg|com)/i.test(href)) acc.discordURL = href;
                else if (!acc.telegramURL && /(t\.me|telegram\.me)/i.test(href)) acc.telegramURL = href;
                else if (!acc.youtubeURL && /(youtube\.com|youtu\.be)/i.test(href)) acc.youtubeURL = href;
                else if (!acc.linkedinURL && /(linkedin\.com|lnkd\.in)/i.test(href)) acc.linkedinURL = href;
                else if (!acc.redditURL && /reddit\.com/i.test(href)) acc.redditURL = href;
                else if (!acc.mediumURL && /medium\.com/i.test(href)) acc.mediumURL = href;
                else if (!acc.githubURL && /github\.com/i.test(href)) acc.githubURL = href;
                if (/twitter\.com|x\.com/i.test(href)) twitterAll.add(href);
              }
            }
          }
        } catch {}

        return { ...acc, twitterAll: Array.from(twitterAll) };
      }, url);

      // нормализация twitter → x.com
      if (socials.twitterURL) socials.twitterURL = normalizeTwitter(socials.twitterURL || '');
      if (Array.isArray(socials.twitterAll)) {
        const filt = socials.twitterAll
          .map(u => normalizeTwitter(u || ''))
          .filter(u => /^https?:\/\/(?:www\.)?(?:x\.com|twitter\.com)\/[A-Za-z0-9_]{1,15}\/?$/.test(u));
        socials.twitterAll = Array.from(new Set(filt));
      }

      // для обратной совместимости возвращаем еще и html/text (если запрошено)
      let bodyHtml = null;
      let bodyText = null;

      // всегда возвращаем HTML для пост-обработки (в т.ч. когда --socials)
      if (html || opts.socials) {
        try { bodyHtml = await page.content(); } catch {}
      }
      if (text || (!html && !opts.socials)) {
        try { bodyText = await page.evaluate(() => document.body?.innerText || ''); } catch {}
      }

      // после bodyHtml/bodyText - до return:
      const title = await page.title().catch(() => '');

      // заголовки ответа
      const headersObj = {};
      if (resp) {
        try {
          for (const [k, v] of Object.entries(resp.headers() || {})) {
            headersObj[k] = v;
          }
        } catch {}
      }

      // куки из контекста
      const cookiesOut = await context.cookies().catch(() => []);

      // антибот/CF телеметрия
      const antiBot = await detectAntiBot(page, resp);

      // тайминги
      const timing = {
        startedAt,
        finishedAt: Date.now(),
        ms: Date.now() - startedAt,
      };

      return {
        ok, status, url, finalUrl, title,
        html: bodyHtml, text: bodyText,
        headers: headersObj, cookies: cookiesOut,
        console: consoleLogs, timing, antiBot,
        websiteURL: url, ...socials,
      };
} finally {
  try { await page?.close(); } catch {}
  try { await context?.close(); } catch {}
  try { await browser?.close(); } catch {}
}
  };

  // ретраим всю попытку при фатальном падении
  let lastError = null;
  for (let i = 0; i < Math.max(1, retries); i++) {
    try {
      const res = await attempt();
      return res;
    } catch (e) {
      lastError = e;
    }
  }
  // если все попытки упали - возвращаем структурированную ошибку
  return {
    ok: false,
    status: 0,
    url,
    finalUrl: url,
    title: '',
    html: null,
    text: null,
    headers: {},
    cookies: [],
    console: consoleLogs,
    timing: { error: String(lastError && (lastError.message || lastError)) },
    antiBot: { detected: false, kind: '', server: '' },
    websiteURL: url,
  };
}

// CLI-режим: прочитать аргументы, выполнить, вывести JSON
async function main() {
  if (require.main !== module) return;
  const args = parseArgs(process.argv);
  try {
    const result = await browserFetch(args);
    process.stdout.write(JSON.stringify(result, null, 2));
  } catch (e) {
    process.stdout.write(JSON.stringify({
      ok: false,
      status: 0,
      url: args.url || null,
      error: String(e && (e.message || e)),
    }));
    process.exitCode = 1;
  }
}

module.exports = { browserFetch, parseArgs };
main();
