/**
 * EveBot 视觉验证脚本：截取静态待机 / 悬停挥手 / 挥手中段 三个状态的机器人特写。
 * 用法：node scripts/robot-shot.mjs [outDir]（ROBOT_BASE_URL 可指定环境）
 */
import { chromium } from "playwright";

const OUT = process.argv[2] || ".robot-shots";
const URL = process.env.ROBOT_BASE_URL || "http://localhost:3210";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, colorScheme: "dark" });
// 跳过开场页与进场问好，先拍确定性的静态姿势
await page.addInitScript(() => {
  sessionStorage.setItem("bl-intro-seen", "1");
  sessionStorage.setItem("bl-sprite-greeted", "1");
  sessionStorage.setItem("yt-teaser", "1");
});
await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 90000 });
await page.waitForSelector(".ai-sprite-container", { timeout: 60000 });
await page.waitForTimeout(3500);

// 机器人容器特写区域（右下角 420×420）
const clip = { x: 1440 - 420, y: 900 - 460, width: 420, height: 460 };

await page.screenshot({ path: `${OUT}/1-idle.png`, clip });

// 悬停触发挥手：抬臂完成但手指刚开始展开
const bot = page.locator(".ai-sprite-container [role='button']");
await bot.hover();
await page.waitForTimeout(450);
await page.screenshot({ path: `${OUT}/2-wave-early.png`, clip });

// 挥手中段：五指全开 + 腕部摆动
await page.waitForTimeout(900);
await page.screenshot({ path: `${OUT}/3-wave-mid.png`, clip });
await page.waitForTimeout(500);
await page.screenshot({ path: `${OUT}/4-wave-late.png`, clip });

// 移开鼠标，确认收手回位
await page.mouse.move(400, 400);
await page.waitForTimeout(1200);
await page.screenshot({ path: `${OUT}/5-idle-back.png`, clip });

// 控制台错误检查
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
await page.waitForTimeout(300);
console.log("console errors:", errors.length ? errors : "none");

await browser.close();
console.log("done ->", OUT);
