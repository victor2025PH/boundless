/**
 * EveBot 性能巡检：真实页面上分阶段采样帧耗时（rAF 间隔）：
 * A 静置待机（漂浮/眨眼/光池） B 悬停挥手（五指展开+腕摆） C 连续滚动（拖拽/俯仰/避让）
 * D 对照组：移除机器人后再测待机 —— A 与 D 的差值才是机器人的真实开销。
 * 注意：headless 无 GPU 时全站背景特效走软件光栅，绝对值远差于真实浏览器，
 * 只应比较相对差值；绝对性能请在真机 DevTools 复核。
 * 用法：node scripts/robot-perf.mjs（ROBOT_BASE_URL 可指定环境）
 */
import { chromium } from "playwright";

const BASE = process.env.ROBOT_BASE_URL || "http://localhost:3210";

const browser = await chromium.launch({ args: ["--enable-gpu", "--use-angle=d3d11"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, colorScheme: "dark" });
await page.addInitScript(() => {
  sessionStorage.setItem("bl-intro-seen", "1");
  sessionStorage.setItem("bl-sprite-greeted", "1");
  sessionStorage.setItem("yt-teaser", "1");
});
await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 90000 });
const bot = page.locator(".ai-sprite-container [role='button']");
await bot.waitFor({ state: "attached", timeout: 60000 });
await page.waitForTimeout(3500); // 等入场/问好流程结束，进入稳态

/** 页面内 rAF 采样器：收集 ms 毫秒内的帧间隔 */
const sample = (ms) =>
  page.evaluate(
    (dur) =>
      new Promise((resolve) => {
        const deltas = [];
        let last = performance.now();
        const end = last + dur;
        const tick = (t) => {
          deltas.push(t - last);
          last = t;
          if (t < end) requestAnimationFrame(tick);
          else resolve(deltas.slice(1));
        };
        requestAnimationFrame(tick);
      }),
    ms
  );

const stat = (label, arr) => {
  if (!arr.length) {
    console.log(`${label}: no frames`);
    return null;
  }
  const sorted = [...arr].sort((a, b) => a - b);
  const mean = arr.reduce((a, b) => a + b, 0) / arr.length;
  const p95 = sorted[Math.floor(sorted.length * 0.95)];
  const max = sorted[sorted.length - 1];
  const jank = arr.filter((d) => d > 32).length;
  console.log(
    `${label}: frames=${arr.length} mean=${mean.toFixed(1)}ms p95=${p95.toFixed(1)}ms max=${max.toFixed(1)}ms >32ms=${jank} (${((jank / arr.length) * 100).toFixed(1)}%)`
  );
  return { mean, p95 };
};

/* A 静置待机 */
const a = stat("A 待机(含机器人)", await sample(5000));

/* B 悬停挥手（五指手掌 + 腕摆循环） */
await bot.hover();
await page.waitForTimeout(700); // 抬臂展指完成，进入摆动循环
stat("B 挥手          ", await sample(4000));
await page.mouse.move(400, 300);
await page.waitForTimeout(800);

/* C 连续滚动（拖拽跟随 + 俯仰 + 避让计算） */
const scrolling = (async () => {
  for (let i = 0; i < 24; i++) {
    await page.mouse.wheel(0, i % 8 < 4 ? 900 : -900);
    await page.waitForTimeout(160);
  }
})();
const scrollFrames = await sample(4000);
await scrolling;
stat("C 滚动          ", scrollFrames);
await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
await page.waitForTimeout(1200);

/* D 对照组：移除机器人再测待机，A−D 即机器人真实开销 */
await page.evaluate(() => document.querySelector(".ai-sprite-container")?.remove());
await page.waitForTimeout(600);
const d = stat("D 待机(无机器人)", await sample(5000));
if (a && d) {
  console.log(`机器人增量: mean +${(a.mean - d.mean).toFixed(1)}ms p95 +${(a.p95 - d.p95).toFixed(1)}ms（负值=噪声内，无显著开销）`);
}

await browser.close();
console.log("done");
