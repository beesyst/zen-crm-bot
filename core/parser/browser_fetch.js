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
    // fingerprint options
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

// Простейший детектор антибот-страниц (Cloudflare/403/503 и т.п.)
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
  try {
    if (!u) return u;
    const s = String(u);
    // прямой профиль
    let m = s.match(/^https?:\/\/(?:www\.)?(?:twitter\.com|x\.com)\/([A-Za-z0-9_]{1,15})(?:[\/?#].*)?$/i);
    if (m) return `https://x.com/${m[1]}`;

    // intent/follow?screen_name=
    if (/^https?:\/\/(?:www\.)?twitter\.com\/intent\/(?:follow|user)/i.test(s)) {
      const url = new URL(s);
      const screen = (url.searchParams.get('screen_name') || '').trim();
      if (/^[A-Za-z0-9_]{1,15}$/.test(screen)) return `https://x.com/${screen}`;
    }

    // i/flow/login?redirect_after_login=%2F<handle>
    if (s.includes('redirect_after_login')) {
      const url = new URL(s);
      const redir = url.searchParams.get('redirect_after_login') || '';
      const dec = decodeURIComponent(redir || '');
      let m2 = dec.match(/^https?:\/\/(?:www\.)?(?:twitter\.com|x\.com)\/([A-Za-z0-9_]{1,15})(?:[\/?#].*)?$/i);
      if (m2) return `https://x.com/${m2[1]}`;
      let m3 = dec.match(/^\/([A-Za-z0-9_]{1,15})(?:[\/?#].*)?$/);
      if (m3) return `https://x.com/${m3[1]}`;
    }

    // generic query контейнеры
    if (s.includes('?')) {
      const url = new URL(s);
      for (const key of ['url','u','to','target','redirect','redirect_uri']) {
        const cand = url.searchParams.get(key);
        if (cand) {
          const dec = decodeURIComponent(cand);
          const mm = dec.match(/^https?:\/\/(?:www\.)?(?:twitter\.com|x\.com)\/([A-Za-z0-9_]{1,15})(?:[\/?#].*)?$/i);
          if (mm) return `https://x.com/${mm[1]}`;
        }
      }
    }

    // fallback: просто twitter.com → x.com (урезаем хвосты)
    return s.replace(/https?:\/\/(www\.)?twitter\.com/i, 'https://x.com').replace(/[\/?#].*$/, '');
  } catch { return u; }
}

// Построение контекста с отпечатком
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
  // собираем ограничения для FingerprintGenerator
  let devices = undefined;
  if (fpDevice) {
    const d = String(fpDevice).toLowerCase();
    // допустимые: desktop|mobile|tablet
    if (['desktop', 'mobile', 'tablet'].includes(d)) devices = [d];
  }
  let operatingSystems = undefined;
  if (fpOS) {
    const os = String(fpOS).toLowerCase();
    // допустимые: windows|linux|macos|ios|android
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
    if (m) {
      viewport = { width: Number(m[1]), height: Number(m[2]) };
    }
  }

  // Генерируем кастомный отпечаток, если есть хоть одно ограничение или задан UA
  if (devices || operatingSystems || locales || viewport || ua) {
    try {
      const fg = new FingerprintGenerator({
        browsers: [{ name: 'chrome' }],
        devices: devices || ['desktop'],
        operatingSystems: operatingSystems || ['windows', 'linux'],
        locales: locales,
      });

      const { fingerprint } = fg.getFingerprint({ url: targetUrl });

      // принудительно переопределим UA/viewport/locale, если заданы флагами
      const finalUA = ua || fingerprint.userAgent;
      const finalViewport = viewport || fingerprint.viewport || { width: 1366, height: 768 };
      const finalLocale = (locales && locales[0]) || (fingerprint.languages && fingerprint.languages[0]) || 'en-US';

      // создаем контекст с инжекцией данного отпечатка
      const context = await newInjectedContext(browser, {
        fingerprint,
        newContextOptions: {
          userAgent: finalUA,
          viewport: finalViewport,
          locale: finalLocale,
          javaScriptEnabled: js !== false,
          ignoreHTTPSErrors: true,
          bypassCSP: true,
          extraHTTPHeaders: { ...headers, 'Accept-Language': finalLocale + ',en;q=0.9' },
        },
      });
      return context;
    } catch (e) {
      // Если что-то пошло не так — упадём в дефолтный путь ниже.
    }
  }

  // Дефолтный путь: пусть fingerprint-injector сам генерит и инжектит отпечаток
  try {
    return await newInjectedContext(browser, {
      newContextOptions: {
        userAgent: ua || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        javaScriptEnabled: js !== false,
        ignoreHTTPSErrors: true,
        bypassCSP: true,
        viewport: { width: 1366, height: 768 },
        extraHTTPHeaders: { ...headers, 'Accept-Language': 'en-US,en;q=0.9' },
      },
    });
  } catch {
    // Фолбэк - чистый Playwright без инжекции (хуже маскируется, но работает)
    return await browser.newContext({
      userAgent: ua || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
      javaScriptEnabled: js !== false,
      ignoreHTTPSErrors: true,
      bypassCSP: true,
      viewport: { width: 1366, height: 768 },
      extraHTTPHeaders: { ...headers, 'Accept-Language': 'en-US,en;q=0.9' },
    });
  }
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
    fpDevice,
    fpOS,
    fpLocales,
    fpViewport,
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
      // контекст с инжектированным отпечатком (через fingerprint-generator при необходимости)
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

      // куки
      if (Array.isArray(cookies) && cookies.length) {
        try { await context.addCookies(cookies); } catch {}
      }

      const page = await context.newPage();
      page.on('console', (msg) => {
        try { consoleLogs.push({ type: msg.type(), text: msg.text() }); } catch {}
      });

      // надежная навигация
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

      // легкий скролл для прогрева SPA
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

      // подождать футер/социалки и/или JSON-LD - часто SPA дорисовывает
      try {
        await page.waitForSelector(
          'footer a[href], [role="contentinfo"] a[href], [class*="social"] a[href]',
          { timeout: 2500 }
        );
      } catch {}

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

      // скрин по запросу
      if (screenshot) {
        try { await page.screenshot({ path: screenshot, fullPage: true }); } catch {}
      }

      const finalUrl = page.url();
      let status = 0;
      try { status = resp ? (typeof resp.status === 'function' ? resp.status() : 0) : 0; } catch {}
      const ok = true;

      // извлечение соц-ссылок
      const socials = await page.evaluate((base) => {
        const rxTwitter = /twitter\.com|x\.com/i;
        const patterns = {
          twitter: rxTwitter,
          discord: /discord\.gg|discord\.com/i,
          telegram: /t\.me|telegram\.me/i,
          youtube: /youtube\.com|youtu\.be/i,
          linkedin: /linkedin\.com|lnkd\.in/i,
          reddit: /reddit\.com/i,
          medium: /medium\.com/i,
          github: /github\.com/i,
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

        document.querySelectorAll('a[href]').forEach(a => {
          const raw = (a.getAttribute('href') || '').trim();
          if (!raw || raw === '#' || raw.startsWith('#')) return;
          if (raw.startsWith('javascript:') || raw.startsWith('mailto:') || raw.startsWith('tel:')) return;

          const rel = (a.getAttribute('rel') || '').toLowerCase();
          const aria = (a.getAttribute('aria-label') || '').toLowerCase();
          const title = (a.getAttribute('title') || '').toLowerCase();
          const text  = (a.textContent || '').toLowerCase();

          let href = toAbs(unwrapRedirect(raw));

          // кандидаты из data-* и onclick
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
              if (!patterns.discord.test(href) && patterns.discord.test(c)) href = c;
              if (!rxTwitter.test(href) && rxTwitter.test(c)) href = c;
            } catch {}
          }

          // внутренняя "заглушка" /discord
          if (!patterns.discord.test(href) && /(^|\b)discord\b/i.test(raw)) {
            href = toAbs(raw);
          }

          if (!acc.discord && !patterns.discord.test(href) &&
              (text.includes('discord') || aria.includes('discord') || title.includes('discord'))) {
            acc.discord = href;
          }

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
                if (!acc.twitter && /twitter\.com|x\.com/i.test(href)) acc.twitter = href;
                else if (!acc.discord && /discord\.(gg|com)/i.test(href)) acc.discord = href;
                else if (!acc.telegram && /(t\.me|telegram\.me)/i.test(href)) acc.telegram = href;
                else if (!acc.youtube && /(youtube\.com|youtu\.be)/i.test(href)) acc.youtube = href;
                else if (!acc.linkedin && /(linkedin\.com|lnkd\.in)/i.test(href)) acc.linkedin = href;
                else if (!acc.reddit && /reddit\.com/i.test(href)) acc.reddit = href;
                else if (!acc.medium && /medium\.com/i.test(href)) acc.medium = href;
                else if (!acc.github && /github\.com/i.test(href)) acc.github = href;
                if (/twitter\.com|x\.com/i.test(href)) twitterAll.add(href);
              }
            }
          }
        } catch {}

        return { ...acc, twitter_all: Array.from(twitterAll) };
      }, url);

      // нормализация twitter → x.com
      if (socials.twitter) socials.twitter = normalizeTwitter(socials.twitter || '');
      if (Array.isArray(socials.twitter_all)) {
        const filt = socials.twitter_all
          .map(u => normalizeTwitter(u || ''))
          .filter(u => /^https?:\/\/(?:www\.)?(?:x\.com|twitter\.com)\/[A-Za-z0-9_]{1,15}\/?$/.test(u));
        socials.twitter_all = Array.from(new Set(filt));
      }

      // HTML/TEXT опционально
      let bodyHtml = null;
      let bodyText = null;
      if (html || opts.socials) {
        try { bodyHtml = await page.content(); } catch {}
      }
      if (text || (!html && !opts.socials)) {
        try { bodyText = await page.evaluate(() => document.body?.innerText || ''); } catch {}
      }

      const title = await page.title().catch(() => '');

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

      return {
        ok, status, url, finalUrl, title,
        html: bodyHtml, text: bodyText,
        headers: headersObj, cookies: cookiesOut,
        console: consoleLogs, timing, antiBot,
        website: url,
        ...socials,
      };
    } finally {
      // закрытие в finally, чтобы не течь даже при исключениях
      try { await context?.browser()?.close?.(); } catch {}
      try { await context?.close?.(); } catch {}
    }
  };

  // ретраим попытку при фатальном падении
  let lastError = null;
  for (let i = 0; i < Math.max(1, retries); i++) {
    try {
      const res = await attempt();
      return res;
    } catch (e) {
      lastError = e;
    }
  }

  // Структурированная ошибка
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
