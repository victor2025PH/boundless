/**
 * EveBot 性能巡检：真实页面上分三个阶段采样帧耗时（rAF 间隔）：
 * A 静置待机（漂浮/眨眼/光池） B 悬停挥手（五指展开+腕摆） C 连续滚动（拖拽/俯仰/避让）。
 * 输出各阶段 帧数/均值/p95/最大值/超32ms帧占比。dev 模式数值偏悲观，用于相对比较。
 * 用法：node scripts/robot-perf.mjs（ROBOT_BASE_URL 可指定环境）
 */
import { chromium } from "playwright";

const BASE = process.env.ROBOT_BASE_URL || "http://localhost:3210";

const browser = await chromium.launch();
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
  if (!arr.length) return console.log(`${label}: no frames`);
  const sorted = [...arr].sort((a, b) => a - b);
  const mean = arr.reduce((a, b) => a + b, 0) / arr.length;
  const p95 = sorted[Math.floor(sorted.length * 0.95)];
  const max = sorted[sorted.length - 1];
  const jank = arr.filter((d) => d > 32).length;
  console.log(
    `${label}: frames=${arr.length} mean=${mean.toFixed(1)}ms p95=${p95.toFixed(1)}ms max=${max.toFixed(1)}ms >32ms=${jank} (${((jank / arr.length) * 100).toFixed(1)}%)`
  );
};

/* A 静置待机 */
stat("A 待机  ", await sample(5000));

/* B 悬停挥手（五指手掌 + 腕摆循环） */
await bot.hover();
await page.waitForTimeout(700); // 抬臂展指完成，进入摆动循环
stat("B 挥手  ", await sample(4000));
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
stat("C 滚动  ", scrollFrames);

await browser.close();
console.log("done");
