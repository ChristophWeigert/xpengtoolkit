#!/usr/bin/env node
/**
 * Solve the XPENG slider captcha, in a browser, as a human.
 *
 * Renders the interactive NetEase captcha (`initNECaptcha`) at XPENG's own
 * login origin, a person drags the slider, and the resulting `validate` string
 * is printed. Nothing here automates or bypasses the captcha: the account owner
 * clears their own challenge, exactly as they would in the app.
 *
 * Output (stdout, one JSON line):
 *   {"ok":true,"validate":"…","captchaId":"f6c1…"}
 *
 * Diagnostics go to stderr, so stdout stays parseable.
 *
 * Usage:
 *     node tools/solve_captcha.mjs                      # app captchaId, embedded
 *     node tools/solve_captcha.mjs --captcha-id <id>
 *     node tools/solve_captcha.mjs --mode popup
 *     node tools/solve_captcha.mjs --timeout 300
 *
 * Env:
 *     XPENG_PLAYWRIGHT_DIR  dir containing node_modules/playwright-core
 *     XPENG_CHROME          Chromium binary to use
 *     XPENG_CAPTCHA_SDK     override the SDK URL
 */

import { createRequire } from 'node:module';
import { existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const require = createRequire(import.meta.url);

// The captcha widget SDK is a different product from the risk SDK in
// guardian_token.mjs. This is the one that renders a draggable slider.
const CAPTCHA_SDK =
  process.env.XPENG_CAPTCHA_SDK || 'https://cstaticdun.126.net/load.min.js';

const APP_CAPTCHA_ID = 'f6c14777879d4a75b683b29be6709db6';
const WEB_CAPTCHA_ID = 'ee799838945f4771aa5d3dc9bea105d2';

const argv = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};
const CAPTCHA_ID = flag('--captcha-id', APP_CAPTCHA_ID);
const MODE = flag('--mode', 'embed');
const TIMEOUT_S = Number(flag('--timeout', 240));
const DIAG_MS = Number(flag('--diag-ms', 12000));

function log(...args) {
  console.error(...args);
}

function emit(payload, code) {
  console.log(JSON.stringify(payload));
  process.exit(code);
}

function resolveChrome() {
  if (process.env.XPENG_CHROME) return process.env.XPENG_CHROME;
  const root = join(process.env.HOME || '', 'Library/Caches/ms-playwright');
  if (!existsSync(root)) return undefined;
  const builds = readdirSync(root).filter((d) => d.startsWith('chromium-')).sort().reverse();
  const relatives = [
    'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
    'chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
    'chrome-linux/chrome',
    'chrome-linux64/chrome',
  ];
  for (const b of builds) {
    for (const rel of relatives) {
      const p = join(root, b, rel);
      if (existsSync(p)) return p;
    }
  }
  return undefined;
}

let chromium;
try {
  const specifier = process.env.XPENG_PLAYWRIGHT_DIR
    ? join(process.env.XPENG_PLAYWRIGHT_DIR, 'node_modules', 'playwright-core')
    : 'playwright-core';
  ({ chromium } = require(specifier));
} catch (err) {
  emit({ ok: false, error: `playwright-core not found (${err.message})` }, 1);
}

const launchOpts = { headless: argv.includes('--headless') };
if (argv.includes('--chrome')) launchOpts.channel = 'chrome';
else launchOpts.executablePath = resolveChrome();

const browser = await chromium
  .launch(launchOpts)
  .catch((err) => emit({ ok: false, error: `could not launch chromium: ${err.message}` }, 1));

try {
  const context = await browser.newContext({
    userAgent:
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
      '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36',
    viewport: { width: 520, height: 900 },
  });
  const page = await context.newPage();

  // Everything the page says, so a silent failure is not actually silent.
  page.on('console', (m) => log(`  [page:${m.type()}] ${m.text().slice(0, 300)}`));
  page.on('pageerror', (e) => log(`  [pageerror] ${String(e).slice(0, 300)}`));
  page.on('requestfailed', (r) =>
    log(`  [reqfail] ${r.url().slice(0, 120)} — ${r.failure()?.errorText}`),
  );

  // Serve our own page at an XPENG origin. The widget needs a real origin —
  // `about:blank` has an opaque one and reading document.cookie throws.
  await page.route('https://login.xpeng.com/__captcha', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'text/html',
      body: `<!doctype html><html><head><meta charset="utf-8">
             <title>XPENG captcha — drag the slider</title>
             <style>body{font:15px -apple-system,system-ui,sans-serif;padding:24px;margin:0}
                    h1{font-size:16px;margin:0 0 6px} p{color:#555;margin:0 0 18px}
                    #ncc{min-height:380px;border:1px dashed #ccc}</style></head>
             <body><h1>Drag the slider to finish</h1>
             <p>Solve it, then close the window.</p><div id="ncc"></div></body></html>`,
    }),
  );

  await page.goto('https://login.xpeng.com/__captcha', { waitUntil: 'domcontentloaded' });

  log(`\nLoading captcha SDK: ${CAPTCHA_SDK}`);
  await page.addScriptTag({ url: CAPTCHA_SDK }).catch((e) => log(`  SDK load error: ${e.message}`));

  const env = await page.evaluate(() => ({
    hasInit: typeof initNECaptcha,
    hasCaptcha: typeof window.NECaptcha,
    globals: Object.keys(window).filter((k) => /captcha|necaptcha|dun|yidun/i.test(k)).slice(0, 20),
  }));
  log(`SDK state: initNECaptcha=${env.hasInit} NECaptcha=${env.hasCaptcha}`);
  if (env.globals.length) log(`Related globals: ${env.globals.join(', ')}`);
  if (env.hasInit !== 'function') {
    log('\nThe slider SDK did not expose initNECaptcha.');
    await browser.close().catch(() => {});
    emit({ ok: false, captchaId: CAPTCHA_ID, error: 'initNECaptcha not available after SDK load' }, 1);
  }

  const mode = argv.includes('--chrome') ? MODE : MODE;
  log(`\nRendering captcha ${CAPTCHA_ID} in mode=${mode}…`);
  log('If a slider appears, DRAG IT to the right.\n');

  // Kick off the captcha and separately poll the DOM, so a widget that never
  // renders reports *why* instead of just hanging until the timeout.
  const verifyPromise = page.evaluate(
    async ([captchaId, mode]) =>
      await new Promise((resolve) => {
        const done = (v) => resolve(v);
        window.__captchaDone = done;
        window.__captchaEvents = [];
        const note = (what, detail) => {
          window.__captchaEvents.push(`${what}: ${String(detail).slice(0, 200)}`);
        };
        try {
          const inst = initNECaptcha({
            captchaId,
            element: '#ncc',
            mode,
            width: '320px',
            onVerify: (err, ret) => {
              if (err) return done({ ok: false, error: `onVerify err: ${String(err).slice(0, 200)}` });
              const validate = ret && ret.validate;
              if (!validate) return done({ ok: false, error: `no validate in ${JSON.stringify(ret)}` });
              done({ ok: true, validate: String(validate) });
            },
            onError: (e) => done({ ok: false, error: `onError: ${String(e).slice(0, 200)}` }),
            onReady: () => note('onReady', 'widget ready'),
          });
          window.__captchaInst = inst;
          setTimeout(() => {
            try {
              if (mode === 'popup' && inst && inst.popUp) inst.popUp();
            } catch (e) {
              note('popUp threw', e && e.message);
            }
          }, 600);
        } catch (e) {
          done({ ok: false, error: `initNECaptcha threw: ${String((e && e.message) || e).slice(0, 200)}` });
        }
      }),
    [CAPTCHA_ID, mode],
  );

  // Diagnostic: what does the DOM actually look like a few seconds in?
  const diag = await Promise.race([
    verifyPromise,
    (async () => {
      await page.waitForTimeout(DIAG_MS);
      const state = await page.evaluate(() => {
        const el = document.querySelector('#ncc');
        return {
          innerHTML_len: el ? el.innerHTML.length : -1,
          iframes: document.querySelectorAll('iframe').length,
          iframe_srcs: Array.from(document.querySelectorAll('iframe'))
            .map((f) => (f.src || '').slice(0, 100))
            .slice(0, 5),
          events: window.__captchaEvents || [],
          body_text: (document.body.innerText || '').slice(0, 200),
        };
      });
      return { __diag: state };
    })(),
  ]);

  if (diag && diag.__diag) {
    log('\nNo verification after ' + DIAG_MS + 'ms. DOM state:');
    log('  #ncc innerHTML length : ' + diag.__diag.innerHTML_len);
    log('  iframes on page       : ' + diag.__diag.iframes + ' ' + JSON.stringify(diag.__diag.iframe_srcs));
    log('  captcha events        : ' + JSON.stringify(diag.__diag.events));
    log('  body text             : ' + JSON.stringify(diag.__diag.body_text));
    log('\nWindow left open for ' + TIMEOUT_S + 's in case the slider is just slow.');
  }

  const result = await Promise.race([
    verifyPromise,
    (async () => {
      await page.waitForTimeout(TIMEOUT_S * 1000);
      return { ok: false, error: `nobody solved it within ${TIMEOUT_S}s` };
    })(),
  ]);

  await browser.close().catch(() => {});
  if (!result.ok) emit({ ...result, captchaId: CAPTCHA_ID }, 1);
  emit({ ok: true, captchaId: CAPTCHA_ID, validate: result.validate }, 0);
} catch (err) {
  await browser.close().catch(() => {});
  emit({ ok: false, error: String((err && err.message) || err).slice(0, 300) }, 1);
}
