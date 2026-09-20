/**
 * 引力三指造型审片：把 /robot-stage 的各队形/各皮肤按指定倍率拍成 PNG，供人眼比对。
 * 用法：node scripts/hand-shots.mjs [outDir]
 * 环境变量 STAGE_BASE_URL 指向 dev（默认 http://localhost:3220）。
 */
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const OUT = process.argv[2] || ".hand-shots";
const BASE = process.env.STAGE_BASE_URL || "http://localhost:3220";
mkdirSync(OUT, { recursive: true });

/** [文件名, query]；scale 4 = 审美审片，scale 1.4 ≈ 首页真实观感 */
const SHOTS = [
  ["x4-idle_base", "mode=idle_base&bg=ink&scale=4"],
  ["x4-idle_wave", "mode=idle_wave&bg=ink&scale=4"],
  ["x4-idle_scan", "mode=idle_scan&bg=ink&scale=4"],
  ["x4-idle_news", "mode=idle_news&bg=ink&scale=4"],
  ["x4-hand_fan", "mode=idle_base&bg=ink&scale=4&hand=fan"],
  ["x4-hand_radar", "mode=idle_base&bg=ink&scale=4&hand=radar"],
  ["x4-hand_tripod", "mode=idle_base&bg=ink&scale=4&hand=tripod&icon=%F0%9F%8E%AC"],
  ["x4-hand_keyboard", "mode=idle_base&bg=ink&scale=4&hand=keyboard"],
  ["x4-hand_burst", "mode=idle_base&bg=ink&scale=4&hand=burst"],
  ["x4-demon_fan", "mode=idle_wave&bg=ink&scale=4&skin=demon"],
  ["x4-loong_base", "mode=idle_base&bg=ink&scale=4&skin=loong"],
  ["x1_4-idle_base", "mode=idle_base&bg=ink&scale=1.4"],
  ["x1_4-idle_wave", "mode=idle_wave&bg=ink&scale=1.4"],
  ["x1_2-idle_base", "mode=idle_base&bg=ink&scale=1.2"],
];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1100, height: 900 }, colorScheme: "dark", deviceScaleFactor: 2 });
page.on("pageerror", (e) => console.error("PAGEERROR", String(e)));

for (const [name, q] of SHOTS) {
  await page.goto(`${BASE}/robot-stage?${q}`, { waitUntil: "networkidle", timeout: 90000 });
  await page.waitForSelector("[data-stage-ready='1']", { timeout: 30000 });
  /* 等队形弹簧落位 + 挥手摆到接近峰值的相位 */
  await page.waitForTimeout(name.includes("wave") ? 3100 : 1400);
  await page.screenshot({ path: `${OUT}/${name}.png` });
  /* 手部特写：4x 档另出一张只裁左手的紧凑图，材质/间隙靠它判 */
  if (name.startsWith("x4") && (await page.locator(".eve-hand").count())) {
    const box = await page.locator(".eve-hand").first().boundingBox();
    if (box) {
      const pad = 46;
      await page.screenshot({
        path: `${OUT}/${name}--hand.png`,
        clip: { x: Math.max(0, box.x - pad), y: Math.max(0, box.y - pad), width: box.width + pad * 2, height: box.height + pad * 2 },
      });
    }
  }
  console.log("shot", name);
}

/* 队形量测：荚间净空隙 / 包围盒展开度，把「挤成一团」变成可回归的数字 */
const measure = async (hand) => {
  await page.goto(`${BASE}/robot-stage?mode=idle_base&bg=ink&scale=4&hand=${hand}`, { waitUntil: "networkidle" });
  await page.waitForSelector("[data-stage-ready='1']");
  await page.waitForTimeout(1500);
  return page.evaluate(() => {
    const pods = [...document.querySelectorAll(".eve-hand [data-pod]")].map((p) => p.getBoundingClientRect());
    if (pods.length < 3) return null;
    const byX = [...pods].sort((a, b) => a.left - b.left);
    const gap = (a, b) => b.left - a.right;
    const w = pods.reduce((s, r) => s + r.width, 0) / pods.length;
    return {
      spread: +(Math.max(...pods.map((r) => r.left + r.width / 2)) - Math.min(...pods.map((r) => r.left + r.width / 2))).toFixed(1),
      podW: +w.toFixed(1),
      podH: +(pods.reduce((s, r) => s + r.height, 0) / pods.length).toFixed(1),
      gaps: [gap(byX[0], byX[1]), gap(byX[1], byX[2])].map((g) => +g.toFixed(1)),
    };
  });
};
for (const h of ["collapsed", "fan", "radar"]) console.log(h, JSON.stringify(await measure(h)));

await browser.close();
console.log("done ->", OUT);
