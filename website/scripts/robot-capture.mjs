/**
 * EveBot IP 素材导出：基于 /robot-stage 舞台页产出
 * - 8 个姿态的透明背景 PNG（3x 高清，可直接叠加到任意物料底图）
 * - 4 个营销动作的 webm 循环视频（品牌深色底，供 TG 广告/开屏/频道贴图）
 * 输出目录 .robot-assets/。用法：node scripts/robot-capture.mjs
 * 环境变量 ROBOT_BASE_URL 可指向任意环境（默认 http://localhost:3210）。
 */
import { chromium } from "playwright";
import { mkdirSync, renameSync } from "fs";
import path from "path";

const BASE = process.env.ROBOT_BASE_URL || "http://localhost:3210";
const OUT = ".robot-assets";
mkdirSync(OUT, { recursive: true });

const STILL_MODES = ["idle_base", "idle_wave", "idle_dance", "idle_scan", "idle_news", "idle_spin", "flying", "falling"];
const VIDEO_MODES = ["idle_base", "idle_wave", "idle_dance", "idle_news"];

const browser = await chromium.launch();

/* ---------- 透明 PNG 静帧 ---------- */
{
  const page = await browser.newPage({ viewport: { width: 1000, height: 1100 }, colorScheme: "dark" });
  for (const mode of STILL_MODES) {
    await page.goto(`${BASE}/robot-stage?mode=${mode}&scale=3&bg=transparent&pool=0`, { waitUntil: "domcontentloaded", timeout: 90000 });
    await page.waitForSelector("[data-stage-ready]", { timeout: 60000 });
    // 等姿态弹簧落定 / 挥手进入循环段（含五指完全展开）
    await page.waitForTimeout(mode === "idle_wave" ? 1800 : mode === "idle_spin" ? 2400 : 1600);
    await page.screenshot({ path: `${OUT}/eve-${mode}@3x.png`, omitBackground: true });
    console.log(`still  eve-${mode}@3x.png`);
  }
  await page.close();
}

/* ---------- webm 循环视频（品牌深色底 + 悬浮光池） ---------- */
for (const mode of VIDEO_MODES) {
  const ctx = await browser.newContext({
    viewport: { width: 720, height: 860 },
    colorScheme: "dark",
    recordVideo: { dir: OUT, size: { width: 720, height: 860 } },
  });
  const page = await ctx.newPage();
  await page.goto(`${BASE}/robot-stage?mode=${mode}&scale=2.4&bg=ink&pool=1`, { waitUntil: "domcontentloaded", timeout: 90000 });
  await page.waitForSelector("[data-stage-ready]", { timeout: 60000 });
  await page.waitForTimeout(7000); // 录 ~7s，覆盖 2-3 个动作循环
  const video = page.video();
  await page.close();
  await ctx.close();
  if (video) {
    const tmp = await video.path();
    const target = path.join(OUT, `eve-${mode}.webm`);
    try {
      renameSync(tmp, target);
      console.log(`video  eve-${mode}.webm`);
    } catch (e) {
      console.log(`video  ${tmp} (rename failed: ${e.message})`);
    }
  }
}

await browser.close();
console.log("done ->", OUT);
