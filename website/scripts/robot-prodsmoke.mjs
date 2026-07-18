/**
 * 生产只读冒烟：不点击、不写入（不收珠/不许愿），只验证关键 UI 可见与零页面错误。
 * 用法：ROBOT_BASE_URL=https://usdt2026.cc node scripts/robot-prodsmoke.mjs
 */
import { chromium } from "playwright";

const BASE = process.env.ROBOT_BASE_URL || "https://usdt2026.cc";
const OUT = ".robot-shots/prod-smoke";
let fail = 0;
const check = (n, ok, x = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${x ? " — " + x : ""}`);
  if (!ok) fail++;
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, colorScheme: "dark" });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
await page.addInitScript(() => {
  sessionStorage.setItem("bl-intro-seen", "1");
  sessionStorage.setItem("bl-sprite-greeted", "1");
  sessionStorage.setItem("yt-teaser", "1");
});

await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 90000 });
await page.waitForSelector(".ai-sprite-container", { timeout: 60000 });
check("机器人挂载", true);
const offerVisible = await page
  .locator(".dragon-pearl-offer")
  .waitFor({ state: "visible", timeout: 20000 })
  .then(() => true)
  .catch(() => false);
check("今日星珠浮现（未点击）", offerVisible);
await page.screenshot({ path: `${OUT}/p1-home.png`, clip: { x: 1440 - 520, y: 900 - 600, width: 520, height: 600 } });

await page.goto(`${BASE}/loong`, { waitUntil: "domcontentloaded", timeout: 90000 });
await page.waitForSelector(".codex-form", { timeout: 30000 });
await page.waitForTimeout(1200);
const formCount = await page.locator(".codex-form").count();
check("图鉴页三形态卡", formCount === 3, `count=${formCount}`);
await page.screenshot({ path: `${OUT}/p2-codex.png`, fullPage: true });

check("零页面错误", errors.length === 0, errors.join(" | ").slice(0, 160));
await browser.close();
console.log(fail === 0 ? "prod smoke passed" : `${fail} FAILED`);
process.exit(fail === 0 ? 0 : 1);
