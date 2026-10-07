// Frame capture for the PagedOut launch video.
//
// Every frame is a pure function of time: the page exposes render(t), we set
// the time, wait for the layout to settle, and screenshot. No CSS animation
// anywhere, because a real animation would advance with wall-clock time and
// each frame would land wherever the screenshot happened to catch it.
//
// Usage:
//   node capture.mjs stills   -> one PNG per storyboard beat + transitions
//   node capture.mjs frames   -> the full 30fps sequence

import { chromium } from 'playwright';
import { mkdirSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const PAGE = 'file://' + join(HERE, 'scene.html');
const W = 1920, H = 1080, FPS = 30;

const mode = process.argv[2] ?? 'stills';
const outDir = join(HERE, mode === 'stills' ? 'stills' : 'frames');
if (!existsSync(outDir)) mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch({ args: ['--force-color-profile=srgb'] });
const page = await browser.newPage({
  viewport: { width: W, height: H },
  deviceScaleFactor: 1,
});
await page.goto(PAGE, { waitUntil: 'networkidle' });

// Webfonts must be resolved before the first capture, or early frames render
// in a fallback face and the type jumps mid-video.
await page.waitForFunction(() => window.__fontsReady === true, { timeout: 20000 });

const duration = await page.evaluate(() => window.DURATION);

if (mode === 'stills') {
  // One inside each scene, plus the mid-transition moments where a dip-to-
  // ground can go wrong.
  const marks = [
    ['01-hook-typing',   1.35],
    ['02-hook-verdict',  3.05],
    ['03-trans-1to2',    3.50],
    ['04-title',         4.60],
    ['05-trans-2to3',    6.00],
    ['06-chain-healthy', 6.90],
    ['07-chain-cascade', 8.90],
    ['08-trans-3to4',    9.50],
    ['09-counter-mid',  10.60],
    ['10-counter-done', 12.80],
    ['11-trans-4to5',   13.50],
    ['12-finding',      15.40],
    ['13-trans-5to6',   17.50],
    ['14-outro',        19.90],
  ];
  for (const [name, t] of marks) {
    await page.evaluate(v => window.render(v), t);
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
    await page.screenshot({ path: join(outDir, `${name}.png`) });
  }
  console.log(`wrote ${marks.length} stills to ${outDir}`);
} else {
  const total = Math.round(duration * FPS);
  for (let i = 0; i < total; i++) {
    const t = i / FPS;
    await page.evaluate(v => window.render(v), t);
    // Two rAFs: the first applies the style writes, the second guarantees the
    // compositor has painted them before the screenshot lands.
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
    await page.screenshot({ path: join(outDir, `f${String(i).padStart(5, '0')}.png`) });
    if (i % 60 === 0) process.stdout.write(`  ${i}/${total}\n`);
  }
  console.log(`wrote ${total} frames to ${outDir}`);
}

await browser.close();
