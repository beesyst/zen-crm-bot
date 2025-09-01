const { chromium } = require('playwright');
const { URL } = require('node:url');

// Helpers
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
  }
  return args;
}

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

function humanCountToNumber(str) {
  if (!str) return null;
  const s = String(str).trim().toLowerCase().replace(/\s/g, '');

  // локали: 1,2 тыс.; 1.2k; 1.2 млн; 1.2m
  const replaceComma = s.replace(',', '.');
  const mK = /(k|тыс|тис|тыс\.)$/.test(replaceComma) ? 1000 : null;
  const mM = /(m|млн|млн\.)$/.test(replaceComma) ? 1_000_000 : null;
  const mB = /(b|млрд|млрд\.)$/.test(replaceComma) ? 1_000_000_000 : null;
  const mul = mK || mM || mB;
  if (mul) {
    const num = parseFloat(replaceComma.replace(/(k|m|b|тыс|тис|млн|млрд|\.)+$/g, ''));
    return isFinite(num) ? Math.round(num * mul) : null;
  }
  const digits = replaceComma.replace(/[^\d]/g, '');
  return digits ? Number(digits) : null;
}

function pickText(el) {
  if (!el) return '';
  return el.textContent?.trim?.() || el.innerText?.trim?.() || '';
}

// Main scraper
async function scrapeTwitterProfile({ handle, url, timeout = 30000, ua, wait = 'domcontentloaded', js = true, retries = 1 }) {
  const username = toHandle(url, handle);
  if (!username) throw new Error('handle or url is required');

  const primaryUrl = `https://x.com/${username}`;
  const fallbackUrl = `https://nitter.net/${username}`;

  const launchArgs = [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
  ];

  const tryOne = async (targetUrl, isNitter = false) => {
    const browser = await chromium.launch({ headless: true, args: launchArgs });
    let context;
    let page;
    try {
      context = await browser.newContext({
        userAgent: ua || undefined,
        javaScriptEnabled: js,
      });
      page = await context.newPage();
      await page.goto(targetUrl, {
        waitUntil: wait === 'nowait' ? undefined : (['load','domcontentloaded','networkidle'].includes(wait) ? wait : 'domcontentloaded'),
        timeout,
      });

      // мини ожидание, чтобы дорисовались блоки
      await page.waitForTimeout(isNitter ? 100 : 500);

      const finalUrl = page.url();

      if (!isNitter && /log(in)?|suspend|account/.test(finalUrl)) {
        // редирект на логин или ограничения - провалим попытку
        throw new Error(`blocked/redirected to ${finalUrl}`);
      }

      const data = isNitter
        ? await extractFromNitter(page, username, finalUrl)
        : await extractFromX(page, username, finalUrl);

      return data;
    } finally {
      try { await page?.close(); } catch {}
      try { await context?.close(); } catch {}
      try { await browser?.close(); } catch {}
    }
  };

  let lastErr = null;
  // пробуем x.com
  for (let i = 0; i < Math.max(1, retries); i++) {
    try {
      const d = await tryOne(primaryUrl, false);
      d.retries = i;
      return d;
    } catch (e) { lastErr = e; }
  }
  // фолбэк nitter
  try {
    const d = await tryOne(fallbackUrl, true);
    d.fallback = 'nitter';
    return d;
  } catch (e) {
    lastErr = e;
    return {
      ok: false,
      handle: username,
      url: primaryUrl,
      finalUrl: primaryUrl,
      error: String(lastErr && lastErr.message || lastErr),
    };
  }
}

async function extractFromX(page, handle, finalUrl) {
  // имя
  const name = await page.locator('div[data-testid="UserName"] span').first().textContent().catch(() => null);
  // био
  const bio = await page.locator('div[data-testid="UserDescription"]').first().textContent().catch(() => null);

  // верификация - значок рядом с именем
  const verified = await page.locator('div[data-testid="UserName"] svg[aria-label*="Verified"], div[data-testid="UserName"] svg[aria-label*="Подтвержден"]').count().catch(() => 0);

  // аватар
  const avatar = await page.locator('img[src*="profile_images"]').first().getAttribute('src').catch(() => null);

  // баннер
  const banner = await page.locator('div[style*="background-image"]')
    .evaluateAll(nodes => {
      for (const n of nodes) {
        const m = String(n.getAttribute('style') || '').match(/url\("?(.*?)"?\)/i);
        if (m && m[1]) return m[1];
      }
      return null;
    })
    .catch(() => null);

  // локация и сайт - блок meta
  const metaTexts = await page.locator('div[data-testid="UserProfileHeader_Items"] span, div[data-testid="UserProfileHeader_Items"] a').allTextContents().catch(() => []);
  let location = null, website = null;
  for (const t of metaTexts) {
    const s = (t || '').trim();
    if (!s) continue;
    if (/^https?:\/\//i.test(s)) website = website || s;
    else if (!location) location = s;
  }

  // счетчики (Following / Followers / Posts/ Tweets)
  const counters = await page.locator('a[href$="/following"], a[href$="/verified_followers"], a[href$="/followers"], a[href$="/posts"], a[href$="/with_replies"]').allTextContents().catch(() => []);
  let following = null, followers = null, tweets = null;
  for (const t of counters) {
    const s = (t || '').replace(/\n/g, ' ').replace(/\s+/g, ' ').trim().toLowerCase();
    if (s.includes('following') || s.includes('подписки')) {
      const m = s.match(/([\d.,\sкккmkmbмлрдмлнтыстис]+)/i);
      if (m) following = humanCountToNumber(m[1]);
    } else if (s.includes('followers') || s.includes('подписчики') || s.includes('подписчиков')) {
      const m = s.match(/([\d.,\sкккmkmbмлрдмлнтыстис]+)/i);
      if (m) followers = humanCountToNumber(m[1]);
    } else if (s.includes('posts') || s.includes('tweets') || s.includes('твиты') || s.includes('посты')) {
      const m = s.match(/([\d.,\sкккmkmbмлрдмлнтыстис]+)/i);
      if (m) tweets = humanCountToNumber(m[1]);
    }
  }

  // последние твиты (best effort)
  const latest = await page.locator('article[data-testid="tweet"]').evaluateAll(nodes => {
    const take = [];
    for (const el of nodes.slice(0, 5)) {
      try {
        const ida = el.querySelector('a[href*="/status/"]');
        const id = ida ? (ida.getAttribute('href').split('/status/')[1] || '').split('?')[0] : null;
        const url = ida ? new URL(ida.getAttribute('href'), 'https://x.com').toString() : null;
        // текст - грубо собираем
        let text = '';
        const textBlocks = el.querySelectorAll('[data-testid="tweetText"]');
        if (textBlocks && textBlocks.length) {
          text = Array.from(textBlocks).map(n => n.textContent || '').join('\n').trim();
        } else {
          text = el.innerText?.trim?.() || '';
        }
        // дата (aria-label на time)
        const time = el.querySelector('time');
        const ts = time ? time.getAttribute('datetime') : null;
        take.push({ id, url, text, ts });
      } catch (_e) { /* noop */ }
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
  };
}

async function extractFromNitter(page, handle, finalUrl) {
  // имя
  const name = await page.locator('div.profile-card-fullname').first().textContent().catch(() => null);
  // био
  const bio = await page.locator('div.profile-bio').first().textContent().catch(() => null);

  // аватар
  let avatar = await page.locator('a.profile-card-avatar img').first().getAttribute('src').catch(() => null);
  if (avatar && avatar.startsWith('/')) avatar = `https://nitter.net${avatar}`;

  // баннер
  let banner = await page.locator('div.profile-banner img').first().getAttribute('src').catch(() => null);
  if (banner && banner.startsWith('/')) banner = `https://nitter.net${banner}`;

  // метаданные (локация/сайт)
  const infoTexts = await page.locator('div.profile-bio + div.profile-fields .profile-field').allTextContents().catch(() => []);
  let location = null, website = null;
  for (const t of infoTexts) {
    const s = (t || '').trim();
    if (!s) continue;
    if (/^https?:\/\//i.test(s)) website = website || s;
    else if (!location) location = s;
  }

  // Счетчики
  let followers = null, following = null, tweets = null;
  const cnts = await page.locator('div.profile-stat-num').allTextContents().catch(() => []);
  if (cnts && cnts.length >= 3) {
    tweets = humanCountToNumber(cnts[0]);
    following = humanCountToNumber(cnts[1]);
    followers = humanCountToNumber(cnts[2]);
  }

  // Последние твиты
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
      } catch (_e) {}
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
    verified: null, // Nitter не даёт надёжно
    counts: { followers, following, tweets },
    images: { avatar, banner },
    latest,
    source: 'nitter',
  };
}

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
      error: String(e && e.message || e),
    }));
    process.exitCode = 1;
  }
}

module.exports = { scrapeTwitterProfile, humanCountToNumber };
main();
