const { URL } = require('node:url');
const { chromium } = require('playwright');
const { FingerprintGenerator } = require('fingerprint-generator');
const { FingerprintInjector } = require('fingerprint-injector');

// CLI-параметры
function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--handle') args.handle = argv[++i];
    else if (a === '--url') args.url = argv[++i];
    else if (a === '--timeout') args.timeout = Number(argv[++i]);
    else if (a === '--ua') args.ua = argv[++i];
    else if (a === '--retries') args.retries = Math.max(0, Number(argv[++i]) || 0);
    else if (a === '--wait') args.wait = argv[++i];
    else if (a === '--js') { args.js = String(argv[++i]).toLowerCase() !== 'false'; }
    else if (a === '--prefer') args.prefer = String(argv[++i] || '').toLowerCase();
  }
  return args;
}

// Получение @handle из url/параметра
function toHandle(inputUrl, handle) {
  if (handle) return handle.replace(/^@/, '').trim();
  if (!inputUrl) return null;
  try {
    const u = new URL(inputUrl);
    const seg = u.pathname.split('/').filter(Boolean)[0] || '';
    return seg.replace(/^@/, '').trim();
  } catch {
    return null;
  }
}

// "1.2k / 1,2 тыс. / 1.2m ..." → число
function humanCountToNumber(str) {
  if (!str) return null;
  const s = String(str).trim().toLowerCase().replace(/\s/g, '');
  const replaceComma = s.replace(',', '.');
  const mK = /(k|тыс|тис|тыс\.)$/.test(replaceComma) ? 1000 : null;
  const mM = /(m|млн|млн\.)$/.test(replaceComma) ? 1_000_000 : null;
  const mB = /(b|млрд|млрд\.)$/.test(replaceComma) ? 1_000_000_000 : null;
  const mul = mK || mM || mB;
  if (mul) {
    const num = parseFloat(replaceComma.replace(/(k|m|b|тыс|тис|млн|млрд|\.)+$/g, ''));
    return Number.isFinite(num) ? Math.round(num * mul) : null;
  }
  const digits = replaceComma.replace(/[^\d]/g, '');
  return digits ? Number(digits) : null;
}

// twitter.com → x.com
function normalizeTwitter(u) {
  try { return u ? u.replace(/https?:\/\/(www\.)?twitter\.com/i, 'https://x.com') : u; }
  catch { return u; }
}

// Абсолютный URL относительно base
function absUrl(href, base) {
  try { return href?.startsWith('http') ? href : new URL(href, base).href; }
  catch { return href; }
}

// Декодирование nitter /pic/... → pbs.twimg.com
function decodeNitterPic(u) {
  try {
    if (!u) return null;
    const s = u.replace(/^https?:\/\/[^/]+/i, '');
    const tail = s.startsWith('/pic/') ? s.slice(5) : s;
    const dec = decodeURIComponent(tail.replace(/^\/+/, ''));
    if (dec.startsWith('http://')) return 'https://' + dec.slice(7);
    if (dec.startsWith('https://')) return dec;
    return 'https://' + dec;
  } catch {
    return null;
  }
}

// Основной скрапер: по умолчанию Nitter → X
async function scrapeTwitterProfile({
  handle,
  url,
  timeout = 30000,
  ua,
  wait = 'domcontentloaded',
  js = true,
  retries = 1,
  prefer = 'nitter',
}) {
  const username = toHandle(url, handle);
  if (!username) throw new Error('handle or url is required');

  const primaryUrl = `https://x.com/${username}`;
  const fallbackUrl = `https://nitter.net/${username}`;

  const launchArgs = [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--disable-blink-features=AutomationControlled',
  ];

  // один проход: запустить хром, сгенерить fingerprint, внедрить, перейти и распарсить
  async function tryOne(targetUrl, isNitter = false) {
    const browser = await chromium.launch({ headless: true, args: launchArgs });
    let context, page;
    try {
      // генератор отпечатков - контролируем типы устройств/ОС под наш таргет
      const fg = new FingerprintGenerator({
        browsers: [{ name: 'chrome' }],
        devices: ['desktop'],
        operatingSystems: ['windows', 'linux'],
      });
      const { fingerprint } = fg.getFingerprint({ url: targetUrl });

      const fpUA = (ua && String(ua)) || fingerprint.userAgent;
      const fpViewport = fingerprint.viewport || { width: 1366, height: 768 };
      const fpLocale  = (fingerprint.languages && fingerprint.languages[0]) || 'en-US';

      context = await browser.newContext({
        userAgent: fpUA,
        viewport: fpViewport,
        locale: fpLocale,
        javaScriptEnabled: js !== false,
        ignoreHTTPSErrors: true,
        bypassCSP: true,
        extraHTTPHeaders: { 'Accept-Language': 'en-US,en;q=0.9' },
      });

      // впрыскиваем сгенерированный отпечаток в контекст
      const injector = new FingerprintInjector();
      await injector.attachFingerprintToPlaywright(context, fingerprint);

      page = await context.newPage();

      // чуть экономим трафик/шум
      await page.route('**/*', (route) => {
        const t = route.request().resourceType();
        if (t === 'image' || t === 'media' || t === 'font') return route.abort();
        return route.continue();
      });

      // робастная навигация: несколько режимов ожидания
      const waitOpt = (wait === 'nowait')
        ? undefined
        : (['load','domcontentloaded','networkidle','commit'].includes(wait) ? wait : 'domcontentloaded');

      const tries = [
        { waitUntil: waitOpt || 'domcontentloaded', timeout },
        { waitUntil: 'load', timeout },
        { waitUntil: 'commit', timeout },
        { waitUntil: 'networkidle', timeout },
      ];
      for (const opt of tries) {
        try {
          await page.goto(targetUrl, opt);
          try { await page.waitForLoadState('networkidle', { timeout: 8000 }); } catch {}
          break;
        } catch { /* следующий режим */ }
      }

      // небольшой автоскролл - на SPA дорисовываются блоки
      try {
        await page.evaluate(async () => {
          const delay = (ms) => new Promise(r => setTimeout(r, ms));
          for (let i = 0; i < 6; i++) {
            window.scrollTo(0, document.body.scrollHeight);
            await delay(200);
          }
        });
      } catch {}

      const finalUrl = page.url();

      // если X увел на логин/челлендж - считаем блокировкой
      if (!isNitter && /(log(in)?|suspend|account|consent|challenge)/i.test(finalUrl)) {
        throw new Error(`blocked/redirected to ${finalUrl}`);
      }

      return isNitter
        ? await extractFromNitter(page, username, finalUrl)
        : await extractFromX(page, username, finalUrl);
    } finally {
      try { await page?.close(); } catch {}
      try { await context?.close(); } catch {}
      try { await browser?.close(); } catch {}
    }
  }

  // порядок обхода: по умолчанию Nitter → X (можно --prefer x)
  let lastErr = null;
  const order = (String(prefer || '').toLowerCase() === 'x')
    ? [{ url: primaryUrl, isNitter: false }, { url: fallbackUrl, isNitter: true }]
    : [{ url: fallbackUrl, isNitter: true }, { url: primaryUrl, isNitter: false }];

  for (const step of order) {
    if (step.isNitter) {
      try {
        const d = await tryOne(step.url, true);
        d.fallback = 'nitter';
        return d;
      } catch (e) { lastErr = e; }
    } else {
      for (let i = 0; i < Math.max(1, retries); i++) {
        try {
          const d = await tryOne(step.url, false);
          d.retries = i;
          return d;
        } catch (e) { lastErr = e; }
      }
    }
  }

  // обе ветки упали
  return {
    ok: false,
    handle: username,
    url: primaryUrl,
    finalUrl: primaryUrl,
    error: String((lastErr && lastErr.message) || lastErr),
  };
}

// Извлечение X (x.com/<handle>)
async function extractFromX(page, handle, finalUrl) {
  const name = await page.locator('div[data-testid="UserName"] span').first().textContent().catch(() => null);
  const bio = await page.locator('div[data-testid="UserDescription"]').first().textContent().catch(() => null);
  const verified = await page
    .locator('div[data-testid="UserName"] svg[aria-label*="Verified"], div[data-testid="UserName"] svg[aria-label*="Подтвержден"]')
    .count()
    .catch(() => 0);
  const avatar = await page.locator('img[src*="profile_images"]').first().getAttribute('src').catch(() => null);

  const banner = await page.locator('div[style*="background-image"]').evaluateAll(nodes => {
    for (const n of nodes) {
      const m = String(n.getAttribute('style') || '').match(/url\("?(.*?)"?\)/i);
      if (m && m[1]) return m[1];
    }
    return null;
  }).catch(() => null);

  const metaTexts = await page
    .locator('div[data-testid="UserProfileHeader_Items"] span, div[data-testid="UserProfileHeader_Items"] a')
    .allTextContents()
    .catch(() => []);
  let location = null, website = null;
  for (const t of metaTexts) {
    const s = (t || '').trim();
    if (!s) continue;
    if (/^https?:\/\//i.test(s)) website = website || s;
    else if (!location) location = s;
  }

  // ссылки из био/шапки
  let links = [];
  try {
    links = await page.evaluate((base) => {
      const out = new Set();
      const toAbs = (h) => { try { return h.startsWith('http') ? h : new URL(h, base).href; } catch { return h; } };
      document.querySelectorAll('div[data-testid="UserDescription"] a[href], div[data-testid="UserProfileHeader_Items"] a[href]').forEach(a => {
        const raw = (a.getAttribute('href') || '').trim();
        if (!raw) return;
        out.add(toAbs(raw));
      });
      return Array.from(out);
    }, finalUrl);
  } catch { links = []; }
  links = Array.from(new Set((links || []).map(u => normalizeTwitter(absUrl(u, finalUrl)))));

  const counters = await page
    .locator('a[href$="/following"], a[href$="/verified_followers"], a[href$="/followers"], a[href$="/posts"], a[href$="/with_replies"]')
    .allTextContents()
    .catch(() => []);
  let following = null, followers = null, tweets = null;
  for (const t of counters) {
    const s = (t || '').replace(/\n/g, ' ').replace(/\s+/g, ' ').trim().toLowerCase();
    if (s.includes('following') || s.includes('подписки')) {
      const m = s.match(/([\d.,\sкккmkmbмлрдмлнтыстис]+)/i);
      if (m) following = humanCountToNumber(m[1]);
    } else if (s.includes('followers') || s.includes('подписчики') || s.includes('подписчиков')) {
      const m = s.match(/([\д.,\sкккmkmbмлрдмлнтыстис]+)/i);
      if (m) followers = humanCountToNumber(m[1]);
    } else if (s.includes('posts') || s.includes('tweets') || s.includes('твиты') || s.includes('посты')) {
      const m = s.match(/([\д.,\sкккmkmbмлрдмлнтыстис]+)/i);
      if (m) tweets = humanCountToNumber(m[1]);
    }
  }

  const latest = await page.locator('article[data-testid="tweet"]').evaluateAll(nodes => {
    const take = [];
    for (const el of nodes.slice(0, 5)) {
      try {
        const ida = el.querySelector('a[href*="/status/"]');
        const id = ida ? (ida.getAttribute('href').split('/status/')[1] || '').split('?')[0] : null;
        const url = ida ? new URL(ida.getAttribute('href'), 'https://x.com').toString() : null;
        let text = '';
        const textBlocks = el.querySelectorAll('[data-testid="tweetText"]');
        if (textBlocks && textBlocks.length) {
          text = Array.from(textBlocks).map(n => n.textContent || '').join('\n').trim();
        } else {
          text = el.innerText?.trim?.() || '';
        }
        const time = el.querySelector('time');
        const ts = time ? time.getAttribute('datetime') : null;
        take.push({ id, url, text, ts });
      } catch {}
    }
    return take;
  }).catch(() => []);

  return {
    ok: true,
    handle,
    url: `https://x.com/${handle}`,
    finalUrl,
    name: name ? name.trim() : null,
    bio: bio ? bio.trim() : null,
    location,
    website,
    verified: !!verified,
    counts: { followers, following, tweets },
    images: { avatar, banner },
    latest,
    links,
  };
}

// Извлечение Nitter (nitter.net/<handle>)
async function extractFromNitter(page, handle, finalUrl) {
  const name = await page.locator('.profile-card-fullname').first().textContent().catch(() => null);
  const bio = await page.locator('div.profile-bio').first().textContent().catch(() => null);

  let avatar =
    await page.locator('a.profile-card-avatar img').first().getAttribute('src').catch(() => null)
    || await page.locator('img[src*="profile_images"]').first().getAttribute('src').catch(() => null)
    || await page.locator('link[rel="preload"][as="image"][href*="profile_images"]').first().getAttribute('href').catch(() => null)
    || await page.locator('meta[property="og:image"], meta[name="og:image"], meta[property="twitter:image:src"]').first().getAttribute('content').catch(() => null);

  if (avatar) {
    if (/^https?:\/\/[^/]+\/pic\//i.test(avatar) || avatar.startsWith('/pic/')) {
      avatar = decodeNitterPic(avatar);
    } else if (avatar.startsWith('/')) {
      avatar = absUrl(avatar, finalUrl);
    }
  }

  let banner = await page.locator('div.profile-banner img').first().getAttribute('src').catch(() => null);
  if (banner) {
    if (/^https?:\/\/[^/]+\/pic\//i.test(banner) || banner.startsWith('/pic/')) {
      banner = decodeNitterPic(banner);
    } else if (banner.startsWith('/')) {
      banner = absUrl(banner, finalUrl);
    }
  }

  let location = null, website = null;
  try {
    website = await page.locator('.profile-website a[href]').first().getAttribute('href').catch(() => null);
  } catch {}
  if (!website) {
    const infoTexts = await page.locator('div.profile-bio + div.profile-fields .profile-field').allTextContents().catch(() => []);
    for (const t of infoTexts) {
      const s = (t || '').trim();
      if (!s) continue;
      if (/^https?:\/\//i.test(s)) { website = website || s; }
      else if (!location) location = s;
    }
  }

  let links = [];
  try {
    links = await page.evaluate((base) => {
      const out = new Set();
      const toAbs = (h) => { try { return h.startsWith('http') ? h : new URL(h, base).href; } catch { return h; } };
      document.querySelectorAll('.profile-website a[href], a[rel="me"], .profile-bio a[href]').forEach(a => {
        const raw = (a.getAttribute('href') || '').trim();
        if (!raw) return;
        let href = raw;
        try {
          const isOut = /^\/(out|redirect|external)\?/.test(href);
          if (isOut) {
            const qs = new URL(href, base).searchParams;
            href = qs.get('url') || qs.get('u') || raw;
          }
        } catch {}
        const abs = toAbs(href);
        if (abs) out.add(abs);
      });
      return Array.from(out);
    }, finalUrl);
  } catch { links = []; }
  links = Array.from(new Set((links || []).map(u => normalizeTwitter(absUrl(u, finalUrl)))));

  const cnts = await page.locator('div.profile-stat-num').allTextContents().catch(() => []);
  let followers = null, following = null, tweets = null;
  if (cnts && cnts.length >= 3) {
    tweets = humanCountToNumber(cnts[0]);
    following = humanCountToNumber(cnts[1]);
    followers = humanCountToNumber(cnts[2]);
  }

  const latest = await page.locator('div.timeline > div.timeline-item').evaluateAll(nodes => {
    const take = [];
    for (const el of nodes.slice(0, 5)) {
      try {
        const a = el.querySelector('a[href*="/status/"]');
        const url = a ? new URL(a.getAttribute('href'), 'https://nitter.net').toString() : null;
        const id = url ? (url.split('/status/')[1] || '').split('?')[0] : null;
        const textEl = el.querySelector('.tweet-content');
        const text = textEl ? textEl.textContent.trim() : (el.innerText || '').trim();
        const time = el.querySelector('span.tweet-date a');
        const ts = time ? time.getAttribute('title') : null;
        take.push({ id, url, text, ts });
      } catch {}
    }
    return take;
  }).catch(() => []);

  return {
    ok: true,
    handle,
    url: `https://x.com/${handle}`,
    finalUrl,
    name: name ? name.trim() : null,
    bio: bio ? bio.trim() : null,
    location,
    website,
    verified: null,
    counts: { followers, following, tweets },
    images: { avatar, banner },
    latest,
    links,
    source: 'nitter',
  };
}

// CLI-обертка
async function main() {
  if (require.main !== module) return;
  const args = parseArgs(process.argv);
  try {
    const res = await scrapeTwitterProfile(args);
    process.stdout.write(JSON.stringify(res, null, 2));
  } catch (e) {
    process.stdout.write(JSON.stringify({
      ok: false,
      handle: toHandle(args.url, args.handle),
      url: args.url || (args.handle ? `https://x.com/${args.handle.replace(/^@/, '')}` : null),
      error: String((e && e.message) || e),
    }));
    process.exitCode = 1;
  }
}

module.exports = { scrapeTwitterProfile, humanCountToNumber };
main();
