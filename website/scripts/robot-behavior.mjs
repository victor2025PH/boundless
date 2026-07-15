/**
 * EveBot 行为回归：进场问好（断言手掌展开）/ 点击空白飞行（断言落点）/ 8s 自动回家（断言归位）/
 * 坠落与飞行姿态拍摄 / 全息播报点击带种子问题开客服 / 移动端轻量版可见可点。
 * 任一断言失败以退出码 1 结束。用法：node scripts/robot-behavior.mjs [outDir]
 * 环境变量 ROBOT_BASE_URL 可指向 staging/生产跑部署后冒烟（默认 http://localhost:3210；
 * 注意：对生产跑会产生少量 sprite_* 埋点事件）。
 */
import { chromium } from "playwright";

const OUT = process.argv[2] || ".robot-shots";
const URL = process.env.ROBOT_BASE_URL || "http://localhost:3210";
const VW = 1440;
const VH = 900;
/** 与组件内 HOME/BOT 常量一致 */
const HOME_CENTER = { x: VW - 24 - 128 + 64, y: VH - 160 - 176 + 88 };

let failed = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failed++;
};
const near = (a, b, tol = 14) => Math.abs(a - b) <= tol;

const browser = await chromium.launch();

/* ================= 桌面端 ================= */
{
  const page = await browser.newPage({ viewport: { width: VW, height: VH }, colorScheme: "dark" });
  await page.addInitScript(() => {
    sessionStorage.setItem("bl-intro-seen", "1");
    sessionStorage.setItem("yt-teaser", "1");
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 90000 });

  const bot = page.locator(".ai-sprite-container [role='button']");
  await bot.waitFor({ state: "attached", timeout: 60000 });
  await page.waitForFunction(
    () => {
      const el = document.querySelector(".ai-sprite-container [role='button']");
      return el && parseFloat(getComputedStyle(el).opacity) > 0.95;
    },
    { timeout: 60000 }
  );

  const clip = { x: VW - 460, y: VH - 560, width: 460, height: 560 };

  // 进场问好（挥手窗口约 [2.8s, 5.6s]，取中段拍摄并断言五指手掌可见）
  await page.waitForTimeout(1900);
  const handOpacity = await page.evaluate(() => {
    const el = document.querySelector(".eve-hand");
    return el ? parseFloat(getComputedStyle(el).opacity) : -1;
  });
  check("进场问好时手掌展开", handOpacity > 0.5, `opacity=${handOpacity}`);
  await page.screenshot({ path: `${OUT}/b1-greet.png`, clip });
  await page.waitForTimeout(3600);

  // 点击空白 → 飞行
  await page.mouse.click(700, 380);
  await page.waitForTimeout(2600);
  const boxFly = await bot.boundingBox();
  const flyC = { x: boxFly.x + boxFly.width / 2, y: boxFly.y + boxFly.height / 2 };
  check("飞行落点≈点击处", near(flyC.x, 700) && near(flyC.y, 380), JSON.stringify(flyC));
  await page.screenshot({ path: `${OUT}/b2-fly.png` });

  // 8s 自动回家
  await page.waitForTimeout(9000);
  const boxHome = await bot.boundingBox();
  const homeC = { x: boxHome.x + boxHome.width / 2, y: boxHome.y + boxHome.height / 2 };
  check("自动回家归位", near(homeC.x, HOME_CENTER.x) && near(homeC.y, HOME_CENTER.y), JSON.stringify(homeC));
  await page.screenshot({ path: `${OUT}/b3-home.png`, clip });

  check("桌面端无页面错误", errors.length === 0, errors.join(" | "));
  await page.close();
}

/* ============ 坠落/飞行姿态（调试锁定拍摄） ============ */
for (const pose of ["falling", "flying"]) {
  const page = await browser.newPage({ viewport: { width: VW, height: VH }, colorScheme: "dark" });
  await page.addInitScript(() => {
    sessionStorage.setItem("bl-intro-seen", "1");
    sessionStorage.setItem("bl-sprite-greeted", "1");
    sessionStorage.setItem("yt-teaser", "1");
  });
  await page.goto(`${URL}/?robot=${pose}`, { waitUntil: "domcontentloaded", timeout: 90000 });
  const bot = page.locator(".ai-sprite-container [role='button']");
  await bot.waitFor({ state: "visible", timeout: 60000 });
  await page.waitForTimeout(2800);
  await page.screenshot({ path: `${OUT}/b4-pose-${pose}.png`, clip: { x: VW - 420, y: VH - 560, width: 420, height: 560 } });
  await page.close();
}

/* ============ 全息播报点击 → 种子问题进客服 ============ */
{
  const page = await browser.newPage({ viewport: { width: VW, height: VH }, colorScheme: "dark" });
  await page.addInitScript(() => {
    sessionStorage.setItem("bl-intro-seen", "1");
    sessionStorage.setItem("bl-sprite-greeted", "1");
    sessionStorage.setItem("yt-teaser", "1");
  });
  await page.goto(`${URL}/?robot=idle_news`, { waitUntil: "domcontentloaded", timeout: 90000 });
  const holo = page.locator(".ai-sprite-container .cursor-pointer.bg-black\\/80");
  await holo.waitFor({ state: "visible", timeout: 60000 });
  await page.screenshot({ path: `${OUT}/b5-hologram.png`, clip: { x: VW - 460, y: VH - 620, width: 460, height: 620 } });
  await holo.click();
  await page.waitForTimeout(1200);
  // 种子直答：无历史对话时自动代发种子问题（CTA 承诺“点我 · 立即咨询”），
  // 断言对话流里出现了该用户消息气泡
  const seedBubble = page.locator("text=介绍一下你们的核心能力").first();
  const seeded = await seedBubble.isVisible().catch(() => false);
  check("播报点击自动代发种子问题", seeded, seeded ? "user bubble visible" : "bubble not found");
  await page.screenshot({ path: `${OUT}/b6-seeded-chat.png` });
  await page.close();
}

/* ================= 移动端轻量版 ================= */
{
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, colorScheme: "dark", isMobile: true, hasTouch: true });
  await page.addInitScript(() => {
    sessionStorage.setItem("bl-intro-seen", "1");
    sessionStorage.setItem("bl-sprite-greeted", "1");
    sessionStorage.setItem("yt-teaser", "1");
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 90000 });
  const bot = page.locator(".ai-sprite-container [role='button']");
  await bot.waitFor({ state: "visible", timeout: 60000 });
  await page.waitForTimeout(2500);
  const box = await bot.boundingBox();
  check("移动端机器人可见且已缩放", !!box && box.width < 100, box ? `w=${Math.round(box.width)}` : "no box");
  await page.screenshot({ path: `${OUT}/b7-mobile-idle.png`, clip: { x: 390 - 240, y: 844 - 420, width: 240, height: 420 } });
  await bot.tap();
  await page.waitForTimeout(900);
  const chatInput = page.locator("input[maxlength='1000']");
  check("移动端点按打开客服", await chatInput.isVisible());
  await page.screenshot({ path: `${OUT}/b8-mobile-chat.png` });
  check("移动端无页面错误", errors.length === 0, errors.join(" | "));
  await page.close();
}

await browser.close();
console.log(failed ? `done with ${failed} FAIL -> ${OUT}` : `all checks passed -> ${OUT}`);
process.exit(failed ? 1 : 0);
