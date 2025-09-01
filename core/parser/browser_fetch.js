const { chromium } = require('playwright');

const WAIT_STATES = new Set(['load', 'domcontentloaded', 'networkidle', 'nowait']);

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--html') args.html = true;
    else if (a === '--text') args.text = true;
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
  }
  return args;
}

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
  ];

  // Лог консоли страницы
  const consoleLogs = [];

  const attempt = async () => {
    const startedAt = Date.now();
    const browser = await chromium.launch({ headless: true, args: launchArgs });
    let context;
    let page;
    try {
      context = await browser.newContext({
        userAgent: ua || undefined,
        javaScriptEnabled: js,
        extraHTTPHeaders: headers,
      });

      if (Array.isArray(cookies) && cookies.length) {
        try { await context.addCookies(cookies); } catch { /* ignore */ }
      }

      page = await context.newPage();
      page.on('console', (msg) => {
        try {
          consoleLogs.push({ type: msg.type(), text: msg.text() });
        } catch { /* ignore */ }
      });

      const resp = await page.goto(url, {
        waitUntil: waitUntil || undefined,
        timeout,
      });

      if (waitUntil === null) {
        // nowait: все равно чуть подождем, чтобы прогрузились редиректы/скрипты
        await page.waitForTimeout(500);
      }

      // Опциональный скриншот
      if (screenshot) {
        await page.screenshot({ path: screenshot, fullPage: true });
      }

      const finalUrl = page.url();
      const status = resp ? resp.status() : 0;
      const ok = status >= 200 && status < 400;

      let bodyHtml = null;
      let bodyText = null;

      if (html) {
        bodyHtml = await page.content();
      }
      if (text || !html) {
        try {
          bodyText = await page.evaluate(() => document.body && document.body.innerText ? document.body.innerText : '');
        } catch {
          bodyText = '';
        }
      }

      const title = await page.title().catch(() => '');
      const headersObj = {};
      if (resp) {
        for (const [k, v] of Object.entries(resp.headers() || {})) {
          headersObj[k] = v;
        }
      }
      const cookiesOut = await context.cookies().catch(() => []);

      const timing = {
        startedAt,
        finishedAt: Date.now(),
        ms: Date.now() - startedAt,
      };

      return {
        ok,
        status,
        url,
        finalUrl,
        title,
        html: bodyHtml,
        text: bodyText,
        headers: headersObj,
        cookies: cookiesOut,
        console: consoleLogs,
        timing,
      };
    } finally {
      try { await page?.close(); } catch {}
      try { await context?.close(); } catch {}
      try { await browser?.close(); } catch {}
    }
  };

  let lastError = null;
  for (let i = 0; i < Math.max(1, retries); i++) {
    try {
      const res = await attempt();
      // если страница совсем пустая — можно повторить
      if (!res.ok && i + 1 < retries) continue;
      return res;
    } catch (e) {
      lastError = e;
      if (i + 1 >= retries) {
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
          timing: { error: String(e && e.message || e) },
        };
      }
    }
  }
  // теоретически не дойдем
  throw lastError || new Error('unknown error');
}

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
      error: String(e && e.message || e),
    }));
    process.exitCode = 1;
  }
}

module.exports = { browserFetch, parseArgs };
main();
