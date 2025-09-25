const { chromium } = require('playwright');
const { newInjectedContext } = require('fingerprint-injector');
const { FingerprintGenerator } = require('fingerprint-generator');

// Режимы ожидания навигации
const WAIT_STATES = new Set(['load', 'domcontentloaded', 'networkidle', 'commit', 'nowait']);

// CLI аргументы
function parseArgs(argv) {
  const args = {};
  let positionalUrl = null;

  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];

    if (a === '--html') args.html = true;
    else if (a === '--text') args.text = true;
    else if (a === '--socials') args.socials = true;
    else if (a === '--twitterProfile') args.twitterProfile = true;
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
    }
    // fingerprint options (все опционально)
    else if (a === '--fp-device') args.fpDevice = argv[++i];           // desktop|mobile|tablet
    else if (a === '--fp-os') args.fpOS = argv[++i];                   // windows|linux|macos|ios|android
    else if (a === '--fp-locales') args.fpLocales = argv[++i];         // "en-US,ru-RU"
    else if (a === '--fp-viewport') args.fpViewport = argv[++i];       // "1366x768"
    else if (!a.startsWith('-') && !positionalUrl) {
      // Поддержка вызова: node browser_fetch.js <URL> [--raw]
      positionalUrl = a;
    }
  }

  if (!args.url && positionalUrl) args.url = positionalUrl;
  return args;
}

// Простая эвристика антибот-страниц
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

// Контекст с отпечатком
async function buildContextWithFingerprint(browser, {
  targetUrl,
  ua,
  js,
  headers,
  fpDevice,
  fpOS,
  fpLocales,
  fpViewport,
}) {
  let devices = undefined;
  if (fpDevice) {
    const d = String(fpDevice).toLowerCase();
    if (['desktop', 'mobile', 'tablet'].includes(d)) devices = [d];
  }
  let operatingSystems = undefined;
  if (fpOS) {
    const os = String(fpOS).toLowerCase();
    if (['windows', 'linux', 'macos', 'ios', 'android'].includes(os)) operatingSystems = [os];
  }
  let locales = undefined;
  if (fpLocales) {
    const arr = String(fpLocales).split(',').map(s => s.trim()).filter(Boolean);
    if (arr.length) locales = arr;
  }
  let viewport = undefined;
  if (fpViewport) {
    const m = String(fpViewport).match(/^(\d+)\s*x\s*(\d+)$/i);
    if (m) viewport = { width: Number(m[1]), height: Number(m[2]) };
  }

  // вариант 1: принудительная генерация отпечатка
  if (devices || operatingSystems || locales || viewport || (ua && String(ua).trim())) {
    try {
      const fg = new FingerprintGenerator({
        browsers: [{ name: 'chrome' }],
        devices: devices || ['desktop'],
        operatingSystems: operatingSystems || ['windows', 'linux'],
        locales: locales,
      });

      const { fingerprint } = fg.getFingerprint({ url: targetUrl });

      const finalViewport = viewport || fingerprint.viewport || { width: 1366, height: 768 };
      const finalLocale = (locales && locales[0]) || (fingerprint.languages && fingerprint.languages[0]) || 'en-US';

      const newContextOptions = {
        ...(ua && String(ua).trim() ? { userAgent: ua } : {}),
        viewport: finalViewport,
        locale: finalLocale,
        javaScriptEnabled: js !== false,
        ignoreHTTPSErrors: true,
        bypassCSP: true,
        extraHTTPHeaders: { ...headers, 'Accept-Language': finalLocale + ',en;q=0.9' },
      };

      return await newInjectedContext(browser, { fingerprint, newContextOptions });
    } catch (e) {
    }
  }

  // вариант 2: инжектор сам сгенерит отпечаток
  try {
    const baseOptions = {
      ...(ua && String(ua).trim() ? { userAgent: ua } : {}),
      javaScriptEnabled: js !== false,
      ignoreHTTPSErrors: true,
      bypassCSP: true,
      viewport: { width: 1366, height: 768 },
      extraHTTPHeaders: { ...headers, 'Accept-Language': 'en-US,en;q=0.9' },
    };
    return await newInjectedContext(browser, { newContextOptions: baseOptions });
  } catch {
    // вариант 3: чистый Playwright без инжекции
    const ctx = {
      ...(ua && String(ua).trim() ? { userAgent: ua } : {}),
      javaScriptEnabled: js !== false,
      ignoreHTTPSErrors: true,
      bypassCSP: true,
      viewport: { width: 1366, height: 768 },
      extraHTTPHeaders: { ...headers, 'Accept-Language': 'en-US,en;q=0.9' },
    };
    return await browser.newContext(ctx);
  }
}

// Основная функция: фетч DOM/текста + метаданных
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
    fpDevice,
    fpOS,
    fpLocales,
    fpViewport,
    twitterProfile = false,
  } = opts || {};

  if (!url) throw new Error('url is required');

  const waitUntil = WAIT_STATES.has(wait) ? (wait === 'nowait' ? null : wait) : 'domcontentloaded';

  const launchArgs = [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--disable-blink-features=AutomationControlled',
  ];

  const consoleLogs = [];

  const attempt = async () => {
    const startedAt = Date.now();
    const browser = await chromium.launch({ headless: true, args: launchArgs });
    let context;
    try {
      // контекст с инжектированным отпечатком
      context = await buildContextWithFingerprint(browser, {
        targetUrl: url,
        ua,
        js,
        headers,
        fpDevice,
        fpOS,
        fpLocales,
        fpViewport,
      });

      // куки (если нужны)
      if (Array.isArray(cookies) && cookies.length) {
        try { await context.addCookies(cookies); } catch {}
      }

      const page = await context.newPage();
      page.on('console', (msg) => {
        try { consoleLogs.push({ type: msg.type(), text: msg.text() }); } catch {}
      });

      // надежная навигация с несколькими вариантами waitUntil
      async function robustGoto(p, targetUrl) {
        const tries = [
          { waitUntil: waitUntil || 'domcontentloaded', timeout },
          { waitUntil: 'load',                           timeout },
          { waitUntil: 'commit',                         timeout },
          { waitUntil: 'networkidle',                    timeout },
        ];
        for (const opt of tries) {
          try {
            const r = await p.goto(targetUrl, opt);
            try { await p.waitForLoadState('networkidle', { timeout: 12000 }); } catch {}
            return r;
          } catch (_e) { /* следующий режим */ }
        }
        return null;
      }

      const resp = await robustGoto(page, url);

      // софт прогрев SPA (скролл) - без знания о конкретных доменах
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

      // снимок по запросу
      if (screenshot) {
        try { await page.screenshot({ path: screenshot, fullPage: true }); } catch {}
      }

      const finalUrl = page.url();
      let status = 0;
      try { status = resp ? (typeof resp.status === 'function' ? resp.status() : 0) : 0; } catch {}
      const ok = true;

      // возврат HTML/TEXT по запросу
      let bodyHtml = null;
      let bodyText = null;
      if (html || opts?.socials) {
        try { bodyHtml = await page.content(); } catch {}
      }
      if (text || (!html && !opts?.socials)) {
        try { bodyText = await page.evaluate(() => document.body?.innerText || ''); } catch {}
      }

      const title = await page.title().catch(() => '');

      // проксируем заголовки ответа
      const headersObj = {};
      if (resp) {
        try {
          for (const [k, v] of Object.entries(resp.headers() || {})) {
            headersObj[k] = v;
          }
        } catch {}
      }

      const cookiesOut = await context.cookies().catch(() => []);
      const antiBot = await detectAntiBot(page, resp);
      const timing = { startedAt, finishedAt: Date.now(), ms: Date.now() - startedAt };

      // X-профиль (опционально) - чисто извлечение из DOM страницы X, без нормализаций
      let twitter_profile = null;
      if (twitterProfile && /^https?:\/\/(?:www\.)?(?:x\.com|twitter\.com)\/[A-Za-z0-9_]{1,15}(?:[\/?#].*)?$/i.test(finalUrl)) {
        twitter_profile = await page.evaluate(() => {
          const pick = (sel) => (document.querySelector(sel)?.textContent || '').trim();

          const name = pick('div[data-testid="UserName"] span');
          const bio  = pick('div[data-testid="UserDescription"]');

          const verified = !!document.querySelector('div[data-testid="UserName"] svg[aria-label*="Verified"], div[data-testid="UserName"] svg[aria-label*="Подтвержден"]');

          // avatar
          let avatar = document.querySelector('img[src*="profile_images"]')?.getAttribute('src') || '';
          if (!avatar) {
            const styles = Array.from(document.querySelectorAll('div[style*="background-image"]')).map(n => n.getAttribute('style') || '');
            for (const s of styles) {
              const m = s.match(/url\("?(https?:\/\/pbs\.twimg\.com\/profile_images\/[^")]+)"?\)/i);
              if (m && m[1]) { avatar = m[1]; break; }
            }
          }

          // banner
          let banner = '';
          {
            const nodes = Array.from(document.querySelectorAll('div[style*="background-image"]'));
            for (const n of nodes) {
              const m = String(n.getAttribute('style') || '').match(/url\("?(https?:\/\/pbs\.twimg\.com\/profile_banners\/[^")]+)"?\)/i);
              if (m && m[1]) { banner = m[1]; break; }
            }
          }

          // ссылки из bio и блока UserUrl (под шапкой)
          const entries = [];
          document.querySelectorAll('div[data-testid="UserDescription"] a[href], div[data-testid="UserProfileHeader_Items"] a[href]').forEach(a => {
            const href = (a.getAttribute('href') || '').trim();
            const expanded = (a.getAttribute('data-expanded-url') || a.getAttribute('title') || '').trim();
            const text = (a.textContent || '').trim();
            if (href) entries.push({ href, expanded, text });
          });

          const abs = (h) => { try { return h && h.startsWith('http') ? h : new URL(h, location.href).href; } catch { return h; } };

          function textToUrlMaybe(s) {
            const t = (s || '').trim();
            if (!t) return '';
            if (/^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:\/[^\s]*)?$/.test(t)) {
              return t.startsWith('http') ? t : 'https://' + t;
            }
            return '';
          }

          const links = Array.from(new Set(entries.map(e => {
            if (/^https?:\/\/t\.co\//i.test(e.href) && e.expanded) {
              return abs(e.expanded);
            }
            if (/^https?:\/\/t\.co\//i.test(e.href)) {
              const guess = textToUrlMaybe(e.text);
              if (guess) return abs(guess);
            }
            return abs(e.href);
          })));

          // счетчики
          const countersRaw = Array.from(document.querySelectorAll(
            'a[href$="/following"], a[href$="/verified_followers"], a[href$="/followers"], a[href$="/posts"], a[href$="/with_replies"]'
          )).map(n => (n.textContent || '').replace(/\s+/g,' ').trim().toLowerCase());
          function humanToNum(s) {
            if (!s) return null;
            const x = s.replace(/\s/g,'').replace(',', '.');
            const mul = /(k|тыс|тис|тыс\.)$/.test(x) ? 1e3 : /(m|млн|млн\.)$/.test(x) ? 1e6 : /(b|млрд|млрд\.)$/.test(x) ? 1e9 : null;
            if (mul) {
              const num = parseFloat(x.replace(/(k|m|b|тыс|тис|млн|млрд|\.)+$/g, ''));
              return Number.isFinite(num) ? Math.round(num * mul) : null;
            }
            const d = x.replace(/[^\d]/g, '');
            return d ? Number(d) : null;
          }
          let following=null, followers=null, posts=null;
          for (const t of countersRaw) {
            if (t.includes('following') || t.includes('подписки')) {
              const m = t.match(/([\d.,\sкккmkmbмлрдмлнтыстис]+)/i); if (m) following = humanToNum(m[1]);
            } else if (t.includes('followers') || t.includes('подписчики') || t.includes('подписчиков')) {
              const m = t.match(/([\d.,\sкккmkmbмлрдмлнтыстис]+)/i); if (m) followers = humanToNum(m[1]);
            } else if (t.includes('posts') || t.includes('tweets') || t.includes('твиты') || t.includes('посты')) {
              const m = t.match(/([\д.,\sкккmkmbмлрдмлнтыстис]+)/i); if (m) posts = humanToNum(m[1]);
            }
          }

          return {
            name, bio, verified,
            avatar: avatar || '',
            banner: banner || '',
            links,
            counts: { followers, following, posts },
          };
        });
      }

      // возвращаем сырье: дальше Python разбирает доменную логику (соцсети, JSON-LD и т.п.)
      return {
        ok, status, url, finalUrl, title,
        html: bodyHtml, text: bodyText,
        headers: headersObj, cookies: cookiesOut,
        console: consoleLogs, timing, antiBot,
        website: url,
        ...(twitter_profile ? { twitter_profile } : {}),
      };
    } finally {
      // всегда закрываем
      try { await context?.browser()?.close?.(); } catch {}
      try { await context?.close?.(); } catch {}
    }
  };

  // ретраи при фатальном падении
  let lastError = null;
  for (let i = 0; i < Math.max(1, retries); i++) {
    try {
      const res = await attempt();
      return res;
    } catch (e) {
      lastError = e;
    }
  }

  // структурированная ошибка
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
    website: url,
  };
}

// CLI режим
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
