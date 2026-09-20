/**
 * ============================================================================
 * iOS / 移动端开场片头验收（Playwright WebKit + iPhone 14 Pro）
 * ============================================================================
 *
 * 片头改造（2026-07）后，移动端（pointer: coarse 或视口 <768px）完全不展示开场遮罩：
 * 遮罩内 inline script 在 hydration 前按同一判定先把 #bl-intro 隐藏（防首帧闪现），
 * React effect 随后把它从 DOM 移除、写入会话标记（sessionStorage bl-intro-seen）、
 * 不上滚动锁。本脚本对该契约做移动端验收；旧版「波浪按钮」断言（shapeIsWave /
 * filterApplied / blobMorphAnimating…）已随波浪形状 A/B 下线且移动端不再渲染按钮，
 * 一并移除。
 *
 * 检查项（按执行顺序）：
 *   intro-hidden     首个可断言时刻 #bl-intro 即不可见——不存在，或 display:none
 *                    （后者是 hydration 前 inline script 的合法中间态）
 *   intro-detached   hydration 完成后 #bl-intro 被 React 从 DOM 移除（15s 内）
 *   seen-marker      sessionStorage['bl-intro-seen'] === '1'（移动端等同已看过）
 *   scroll-unlocked  body overflow 未被锁成 hidden（滚动可用）
 *   first-screen     <main> 正文直接可见、页面高度可滚（首屏没有被遮罩挡住）
 *   console-errors   控制台错误（console error + pageerror）共 0 条
 *
 * 用法:
 *   npm run qa:intro-button-ios
 *   node scripts/qa-intro-button-ios.mjs [--url https://bd2026.cc/]
 *
 * 输出：每项检查实时打印一行 {name, pass, detail}，最终打印 {url, engine, checks, pass}；
 *       首屏截图落 scripts/_ios_wave/（目录名沿用旧版，复用 .gitignore 既有条目）。
 * 退出码：0 = 全部通过；1 = 有失败项或脚本异常。
 * ============================================================================
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

const checks = [];
function record(name, pass, detail) {
  checks.push({ name, pass, detail });
  console.log(JSON.stringify({ name, pass, detail }));
}

const browser = await webkit.launch({ headless: true });

try {
  const context = await browser.newContext({ ...phone, locale: "zh-CN" });
  const page = await context.newPage();

  const consoleErrors = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", (err) => consoleErrors.push(String(err?.message || err)));

  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });

  // ---------- 检查 1：intro-hidden —— 首个可断言时刻遮罩即不可见 ----------
  // domcontentloaded 时 React 可能尚未 hydrate：此刻 #bl-intro 若在 DOM，必须已被
  // inline script 置为 display:none（防「先画出来再消失」的黑屏闪现）；hydrate 完成后
  // 则整个节点不存在。两种状态都算隐藏，可见（display 非 none）即 FAIL。
  {
    const state = await page.evaluate(() => {
      const el = document.getElementById("bl-intro");
      if (!el) return { present: false, display: null };
      return { present: true, display: getComputedStyle(el).display };
    });
    const pass = !state.present || state.display === "none";
    record(
      "intro-hidden",
      pass,
      state.present
        ? `#bl-intro 在 DOM 中，display=${JSON.stringify(state.display)}（期望 "none" 或节点不存在）`
        : "#bl-intro 不存在"
    );
  }

  // ---------- 检查 2：intro-detached —— React 校正后遮罩移出 DOM ----------
  {
    let detached = true;
    try {
      await page.waitForFunction(() => !document.getElementById("bl-intro"), null, { timeout: 15000 });
    } catch {
      detached = false;
    }
    record(
      "intro-detached",
      detached,
      detached ? "#bl-intro 已从 DOM 移除" : "15s 内 #bl-intro 仍在 DOM（hydration/effect 校正未生效？）"
    );
  }

  // ---------- 检查 3：seen-marker —— 移动端等同已看过 ----------
  {
    const marker = await page.evaluate(() => {
      try {
        return sessionStorage.getItem("bl-intro-seen");
      } catch {
        return null;
      }
    });
    record("seen-marker", marker === "1", `sessionStorage['bl-intro-seen']=${JSON.stringify(marker)}（期望 "1"）`);
  }

  // ---------- 检查 4：scroll-unlocked —— 不上滚动锁 ----------
  {
    const overflow = await page.evaluate(() => getComputedStyle(document.body).overflow);
    record("scroll-unlocked", overflow !== "hidden", `body overflow=${JSON.stringify(overflow)}（期望非 "hidden"）`);
  }

  // ---------- 检查 5：first-screen —— 首屏正文直接可见、页面可滚 ----------
  {
    const info = await page.evaluate(() => {
      const main = document.querySelector("main");
      const rect = main ? main.getBoundingClientRect() : null;
      return {
        hasMain: Boolean(main),
        mainH: rect ? Math.round(rect.height) : 0,
        scrollH: document.documentElement.scrollHeight,
        vh: window.innerHeight,
      };
    });
    const pass = info.hasMain && info.mainH > 0 && info.scrollH > info.vh;
    record(
      "first-screen",
      pass,
      `main=${info.hasMain}（高度 ${info.mainH}px），页面 scrollHeight=${info.scrollH} vs 视口 ${info.vh}（期望正文成型可滚）`
    );
    await page.screenshot({ path: path.join(outDir, "01-first-screen.png"), fullPage: false });
  }

  // ---------- 检查 6：console-errors ----------
  // 稍等片刻，让 hydration 后的异步报错有机会浮出
  await page.waitForTimeout(400);
  {
    const pass = consoleErrors.length === 0;
    record(
      "console-errors",
      pass,
      pass ? "无控制台错误" : `共 ${consoleErrors.length} 条错误，例如: ${consoleErrors.slice(0, 3).join(" | ")}`
    );
  }

  // ---------- 汇总输出 ----------
  const pass = checks.every((c) => c.pass);
  console.log(JSON.stringify({ url, engine: "webkit + iPhone 14 Pro", outDir, checks, pass }, null, 2));
  await browser.close();
  process.exit(pass ? 0 : 1);
} catch (err) {
  console.error("[qa-intro-ios] 脚本执行异常（请确认 --url 可访问）:", err?.message ?? err);
  if (checks.length) console.log(JSON.stringify({ url, checks, pass: false }, null, 2));
  await browser.close().catch(() => {});
  process.exit(1);
}
