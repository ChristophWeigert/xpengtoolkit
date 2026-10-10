#!/usr/bin/env node
/**
 * Mint a NetEase Yidun "Guardian" anti-bot token for XPENG's login.
 *
 * This is the piece that clears the captcha on `pwdLogin`. It runs NetEase's
 * own silent SDK in a real browser — no slider, no human, no interaction — and
 * prints the token as JSON on stdout:
 *
 *     {"ok":true,"appId":"YD00907311582806","token":"U5hDqf5J8T1EZ1AFAALZQP2uOfUG9mPf"}
 *
 * Protocol, observed live (2025-09-30) — recorded here because it is the thing
 * to reimplement in Python if the browser dependency ever needs to go away:
 *
 *   GET  https://ir-sdk.dun.163.com/v4/j/c?p=<appId>&v=2.0.3&vk=<vk>&n=<nonce>
 *        -> {"code":200,"msg":"成功","data":"<blob>","ok":true}
 *   POST https://ir-sdk.dun.163.com/v4/j/up
 *        {"p","v","vk","n","d":"<encrypted blob>"}     (content-type: text/plain)
 *        -> {"code":200,"data":{"ed":"","ed2":"","es":"…","td":"…","tk":"<TOKEN>","ts":…}}
 *
 * `tk` is the token. The `d` field is encrypted by yidun.js, which is the only
 * nontrivial part of doing this without a browser.
 *
 * Usage:
 *     node guardian_token.mjs [appId] [--headful]
 *
 * `--headful` shows a real browser window. Use it on a machine where the
 * NetEase slider can actually be dragged: the silent Guardian path is
 * risk-scored, and when XPENG challenges it the SDK falls back to the slider.
 * Headless has no way to show or solve that, so it just reports the challenge;
 * headful lets a human drag it and still prints the resulting token.
 *
 * Env:
 *     XPENG_PLAYWRIGHT_DIR  directory containing node_modules/playwright-core
 *     XPENG_CHROME          path to a Chromium binary
 *     XPENG_GUARDIAN_TIMEOUT_MS  how long to wait for a token (default below)
 *     XPENG_GUARDIAN_HEADFUL=1   same as --headful
 */

import { createRequire } from 'node:module';
import { existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const require = createRequire(import.meta.url);

const YIDUN_SDK =
  'https://xp-static-eur.oss-eu-central-1.aliyuncs.com/xp-ucenter/staticResource/yidun.js';

// The Guardian appId XPENG uses. The same value appears inside the captured
// HTProtect risk payload shipped in xpeng_api.py, and the web SSO bundle calls
// createNEGuardian with it. The app's own captcha product number
// (YD00458597415740) works too — both were verified to clear pwdLogin.
const DEFAULT_APP_ID = 'YD00907311582806';

const APP_ID = process.argv[2] && !process.argv[2].startsWith('--')
  ? process.argv[2]
  : DEFAULT_APP_ID;
const HEADFUL =
  process.argv.includes('--headful') || process.env.XPENG_GUARDIAN_HEADFUL === '1';
// The Guardian token is risk-scored on the browser environment. Headless
// Chromium scores poorly; a real, installed Google Chrome scores best because
// it carries a genuine profile, fonts and plugin surface.
const USE_CHROME =
  process.argv.includes('--chrome') || process.env.XPENG_GUARDIAN_CHROME === '1';
// Headful usually means a human has to drag a slider, so allow far longer.
const TIMEOUT_MS = Number(
  process.env.XPENG_GUARDIAN_TIMEOUT_MS || (HEADFUL ? 180000 : 20000),
);

function emit(payload, code) {
  console.log(JSON.stringify({ ...payload, appId: APP_ID }));
  process.exit(code);
}

function fail(message) {
  // Keep enough to be actionable. Playwright puts the failing line first and a
  // boxed hint after it, and cutting at 400 characters stopped inside the box,
  // which is why a container launch failure was unreadable for several rounds.
  emit({ ok: false, error: String(message).slice(0, 2000) }, 1);
}

// Where browser builds can live: the Playwright base image publishes
// PLAYWRIGHT_BROWSERS_PATH, a Linux box keeps them under ~/.cache, and macOS
// under ~/Library/Caches. Searching only the macOS path (as this used to) found
// nothing inside the renewal add-on, which left Playwright to its own lookup.
const BROWSER_ROOTS = [
  process.env.PLAYWRIGHT_BROWSERS_PATH,
  process.env.HOME && join(process.env.HOME, '.cache', 'ms-playwright'),
  process.env.HOME && join(process.env.HOME, 'Library', 'Caches', 'ms-playwright'),
].filter(Boolean);

// The `chromium-` prefix deliberately excludes the chromium_headless_shell-*
// build: this launch wants the full browser, and naming it explicitly is what
// the slider-solver add-on does. Letting Playwright choose on its own picks the
// headless shell, which the image does not carry; the launch then fails with a
// bare "browser has been closed" and a boxed "run npx playwright install" hint
// instead of saying which executable was missing.
const CHROME_RELATIVES = [
  'chrome-linux/chrome',
  'chrome-linux64/chrome',
  'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
  'chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
];

function resolveChrome() {
  if (process.env.XPENG_CHROME) return process.env.XPENG_CHROME;
  for (const root of BROWSER_ROOTS) {
    if (!existsSync(root)) continue;
    const builds = readdirSync(root)
      .filter((d) => d.startsWith('chromium-'))
      .sort()
      .reverse();
    for (const build of builds) {
      for (const rel of CHROME_RELATIVES) {
        const candidate = join(root, build, rel);
        if (existsSync(candidate)) return candidate;
      }
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
  fail(
    `playwright-core not found (${err.message}). ` +
      `Install it (npm i playwright-core) or set XPENG_PLAYWRIGHT_DIR`,
  );
}

// Chromium refuses to start its sandbox as root, and a container's /dev/shm is
// normally too small for it: without the first two flags the launch dies before
// the SDK ever loads. Inside the renewal add-on that surfaced as "could not
// launch chromium: Target page, context or browser has been closed", after the
// slider had already been solved. The third flag keeps the browser from
// advertising itself as automated, which matters because the Guardian token is
// risk-scored on the browser environment. This is the same trio the
// slider-solver add-on launches with.
const LAUNCH_ARGS = [
  '--no-sandbox',
  '--disable-dev-shm-usage',
  '--disable-blink-features=AutomationControlled',
];

function launchOptions(extra = {}) {
  return { headless: !HEADFUL, args: LAUNCH_ARGS, ...extra };
}

const launchOpts = launchOptions();
if (USE_CHROME) {
  // Use the real installed Google Chrome rather than the bundled build.
  // executablePath and channel are mutually exclusive, so the bundled lookup
  // below is for the other branch only.
  launchOpts.channel = 'chrome';
} else {
  // Only set executablePath when the lookup actually found something: if it
  // finds nothing, Playwright's own registry lookup gets a chance instead.
  const found = resolveChrome();
  if (found) launchOpts.executablePath = found;
}

/** What the launch actually used, for the error message. */
function launchContext() {
  return [
    `executablePath=${launchOpts.executablePath || '<playwright default>'}`,
    `channel=${launchOpts.channel || '<none>'}`,
    `PLAYWRIGHT_BROWSERS_PATH=${process.env.PLAYWRIGHT_BROWSERS_PATH || '<unset>'}`,
    `HOME=${process.env.HOME || '<unset>'}`,
    `roots=${JSON.stringify(BROWSER_ROOTS)}`,
  ].join(' ');
}
const browser = await chromium
  .launch(launchOpts)
  .catch(async (err) => {
    if (USE_CHROME) {
      // No real Chrome installed — fall back rather than failing the run.
      console.error(`--chrome unavailable (${err.message}); using bundled Chromium`);
      const found = resolveChrome();
      return chromium
        .launch(launchOptions(found ? { executablePath: found } : {}))
        .catch((e2) => fail(`could not launch chromium [${launchContext()}]: ${e2.message}`));
    }
    return fail(`could not launch chromium [${launchContext()}]: ${err.message}`);
  });

if (HEADFUL) {
  console.error(
    `A browser window is open${USE_CHROME ? ' (real Google Chrome)' : ''}. ` +
      'If NetEase shows a slider, DRAG IT to the right.\n' +
      `Waiting up to ${Math.round(TIMEOUT_MS / 1000)}s for a token…`,
  );
}

try {
  const context = await browser.newContext({
    userAgent:
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
      '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36',
    viewport: { width: 430, height: 900 },
  });
  const page = await context.newPage();

  // The SDK checks origin/referer, so land on XPENG's login host first. This is
  // not decoration; it is part of what makes the token credible.
  await page
    .goto('https://login.xpeng.com/', { waitUntil: 'domcontentloaded', timeout: 45000 })
    .catch(() => {});

  await page.addScriptTag({ url: YIDUN_SDK });
  await page.waitForTimeout(800);

  const result = await page.evaluate(
    async ([appId, timeout]) => {
      if (typeof createNEGuardian !== 'function') {
        return { ok: false, error: 'createNEGuardian missing after SDK load' };
      }
      try {
        const r = await createNEGuardian({ appId, timeout }).getToken();
        if (!r || !r.token) return { ok: false, error: `no token in ${JSON.stringify(r)}` };
        return { ok: true, code: r.code, token: r.token };
      } catch (e) {
        return { ok: false, error: String((e && e.message) || e) };
      }
    },
    [APP_ID, TIMEOUT_MS],
  );

  await browser.close().catch(() => {});
  emit(result, result.ok ? 0 : 1);
} catch (err) {
  await browser.close().catch(() => {});
  fail(err && err.message ? err.message : err);
}
