/**
 * iOS / WebKit 波浪按钮核验（Playwright WebKit + iPhone 14 Pro）
 * 用法: npm run qa:intro-button-ios
 *       node scripts/qa-intro-button-ios.mjs [--url https://bd2026.cc/]
 */
import { webkit, devices } from "playwright";
import { mkdirSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(__dirname, "_ios_wave");
mkdirSync(outDir, { recursive: true });

function getArg(name, fallback) {
  const argv = process.argv.slice(2);
  const eq = argv.find((a) => a.startsWith(`--${name}=`));
  if (eq) return eq.slice(name.length + 3);
  const i = argv.indexOf(`--${name}`);
  if (i !== -1 && argv[i + 1] && !argv[i + 1].startsWith("--")) return argv[i + 1];
  return fallback;
}

const url = getArg("url", "https://bd2026.cc/");
const phone = devices["iPhone 14 Pro"];

const browser = await webkit.launch({ headless: true });
const context = await browser.newContext({ ...phone, locale: "zh-CN" });
const page = await context.newPage();

await page.addInitScript(() => {
  try {
    localStorage.setItem("ab_intro_auto_enter", "a");
    localStorage.setItem("ab_intro_btn_shape", "a");
  } catch {}
});

const consoleErrors = [];
page.on("console", (msg) => {
  if (msg.type() === "error") consoleErrors.push(msg.text());
});
page.on("pageerror", (err) => consoleErrors.push(String(err?.message || err)));

await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
const btn = page.locator(".bl-enter-btn");
await btn.waitFor({ state: "visible", timeout: 25000 });
await page.waitForTimeout(4200);

async function probe() {
  return page.evaluate(() => {
    const el = document.querySelector(".bl-enter-btn");
    const shape = document.querySelector(".bl-enter-btn .siri-shape");
    const blob = document.querySelector(".bl-enter-btn .siri-blob");
    const ring = document.querySelector(".bl-enter-btn .siri-ring");
    const label = document.querySelector(".bl-enter-btn .label");
    const disp = document.querySelector(".bl-enter-btn feDisplacementMap");
    if (!el || !shape || !blob) return null;
    const csShape = getComputedStyle(shape);
    const csBlob = getComputedStyle(blob);
    const csRing = ring ? getComputedStyle(ring) : null;
    const csLabel = label ? getComputedStyle(label) : null;
    const rect = el.getBoundingClientRect();
    return {
      ua: navigator.userAgent,
      vw: window.innerWidth,
      vh: window.innerHeight,
      dpr: window.devicePixelRatio,
      dataShape: el.getAttribute("data-shape"),
      dataLite: el.getAttribute("data-lite"),
      dataCharging: el.getAttribute("data-charging"),
      filter: csShape.filter,
      blobRadius: csBlob.borderRadius,
      blobBg: csBlob.backgroundColor,
      ringRadius: csRing?.borderRadius ?? null,
      labelColor: csLabel?.color ?? null,
      dispScale: disp ? disp.getAttribute("scale") : null,
      btnBox: { w: Math.round(rect.width), h: Math.round(rect.height), y: Math.round(rect.top) },
      reducedMotion: matchMedia("(prefers-reduced-motion: reduce)").matches,
      deviceMemory: navigator.deviceMemory ?? null,
      hw: navigator.hardwareConcurrency ?? null,
    };
  });
}

async function clipAround(name) {
  const box = await btn.boundingBox();
  if (!box) return;
  const padX = 48;
  const padY = 56;
  await page.screenshot({
    path: path.join(outDir, name),
    clip: {
      x: Math.max(0, box.x - padX),
      y: Math.max(0, box.y - padY),
      width: Math.min(phone.viewport.width, box.width + padX * 2),
      height: box.height + padY * 2,
    },
  });
}

const t0 = await probe();
await page.screenshot({ path: path.join(outDir, "01-full.png"), fullPage: false });
await clipAround("02-btn-t0.png");
await page.waitForTimeout(2800);
const t1 = await probe();
await clipAround("03-btn-t1.png");

// 按压充能（不松手前截图；随后 pointerup 取消，避免误进正文影响断言）
await btn.dispatchEvent("pointerdown", { pointerType: "touch", buttons: 1 });
await page.waitForTimeout(450);
const charging = await probe();
await clipAround("04-btn-press.png");
await btn.dispatchEvent("pointerup", { pointerType: "touch", buttons: 0 });
await btn.dispatchEvent("pointercancel", { pointerType: "touch", buttons: 0 });

const radiusChanged = t0 && t1 && t0.blobRadius !== t1.blobRadius;
const filterOn =
  t0 &&
  typeof t0.filter === "string" &&
  t0.filter !== "none" &&
  (t0.filter.includes("bl-btn-wave") || t0.filter.includes("url("));

const report = {
  url,
  engine: "webkit + iPhone 14 Pro",
  outDir,
  consoleErrors: consoleErrors.slice(0, 8),
  t0,
  t1,
  charging: charging
    ? { dataCharging: charging.dataCharging, dispScale: charging.dispScale, filter: charging.filter }
    : null,
  assertions: {
    shapeIsWave: t0?.dataShape === "wave",
    filterApplied: !!filterOn,
    blobMorphAnimating: !!radiusChanged,
    notForcedLite: t0?.dataLite !== "1",
    labelReadableColor: !!(t0?.labelColor && t0.labelColor !== "rgba(0, 0, 0, 0)"),
    pressSetsCharging: charging?.dataCharging === "1",
    mobileWaveBoosted: Number(t0?.dispScale) >= 9,
  },
};

console.log(JSON.stringify(report, null, 2));
await browser.close();

const fails = Object.entries(report.assertions).filter(([, v]) => !v);
process.exit(fails.length ? 1 : 0);
