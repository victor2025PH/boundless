/**
 * 龙珠彩蛋回归：七日收珠 → 召唤 → 祈愿 → 兑换码/皮肤 全链路。
 * 依赖 dev 服务器（x-dragon-now 时间模拟头仅非生产环境生效）。
 * 用法：node scripts/robot-dragon.mjs [outDir]（ROBOT_BASE_URL 可指定环境）
 */
import { chromium } from "playwright";

const OUT = process.argv[2] || ".robot-shots/dragon";
const BASE = process.env.ROBOT_BASE_URL || "http://localhost:3210";
const DAY = 86400000;

let passCount = 0;
let failCount = 0;
const check = (name, ok, extra = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? " — " + extra : ""}`);
  if (ok) passCount++;
  else failCount++;
};

const browser = await chromium.launch();
const errors = [];

/* ────────── A. 完整 UI 流程：day1 点珠 + day2..7 API 模拟 + 召唤 + 愿·机缘 ────────── */
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "dark" });
const page = await ctx.newPage();
page.on("pageerror", (e) => errors.push("A: " + e));
await page.addInitScript(() => {
  sessionStorage.setItem("bl-intro-seen", "1");
  sessionStorage.setItem("bl-sprite-greeted", "1");
  sessionStorage.setItem("yt-teaser", "1");
});
await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 90000 });
await page.waitForSelector(".ai-sprite-container", { timeout: 60000 });

/* 今日星珠浮现（状态拉取延迟 1.8s） */
const offer = page.locator(".dragon-pearl-offer");
await offer.waitFor({ state: "visible", timeout: 45000 });
check("今日星珠浮现", true);
await page.screenshot({ path: `${OUT}/a1-offer.png`, clip: { x: 1440 - 460, y: 900 - 560, width: 460, height: 560 } });

const t0 = Date.now();
await offer.click();
await page.waitForSelector(".dragon-tray", { timeout: 8000 });
check("收珠后托盘出现", true);
let offerGone = true;
try {
  await offer.waitFor({ state: "hidden", timeout: 5000 });
} catch {
  offerGone = false;
}
check("收珠后星珠隐去", offerGone);

/* day2..day7：同 cookie 直接调 API，用 x-dragon-now 模拟未来日期 */
for (let d = 1; d < 7; d++) {
  const r = await ctx.request.post(`${BASE}/api/dragon`, {
    data: { action: "collect" },
    headers: { "x-dragon-now": String(t0 + d * DAY) },
  });
  const j = await r.json();
  if (d < 6 && !(j.ok && j.index === d + 1)) check(`第${d + 1}珠 API`, false, JSON.stringify(j).slice(0, 120));
}
const stR = await ctx.request.get(`${BASE}/api/dragon`, { headers: { "x-dragon-now": String(t0 + 6 * DAY) } });
const st = (await stR.json()).state;
check("七珠集齐可召唤", st.collected === 7 && st.canSummon === true, `collected=${st.collected}`);
check("连续七日北斗正位", st.perfect === true, `streak=${st.streak}`);

/* 重载 → 托盘带 🐉 → 打开召唤仪式（canvas 界龙演出 → 跳过 → 祈愿卡） */
await page.reload({ waitUntil: "domcontentloaded" });
await page.waitForSelector(".ai-sprite-container", { timeout: 60000 });
await page.waitForSelector(".dragon-tray", { timeout: 20000 });
await page.locator(".dragon-tray").click();
await page.waitForTimeout(1400); // 界龙飞行中段
const loongPixels = await page.evaluate(() => {
  const c = [...document.querySelectorAll("canvas")].find((x) => x.closest('[class*="z-[300]"]'));
  if (!c) return -1;
  try {
    const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
    let n = 0;
    for (let i = 3; i < d.length; i += 4) if (d[i] > 8) n++;
    return n;
  } catch {
    return -2;
  }
});
check("界龙 canvas 演出绘制中", loongPixels > 500, `loongPixels=${loongPixels}`);
await page.screenshot({ path: `${OUT}/a2-flight.png` });
/* 收束高潮：2.4s 起同源祥龙本尊（LoongHero）金光现身（轮询避时序竞态） */
let heroOn = false;
for (let i = 0; i < 10 && !heroOn; i++) {
  heroOn = await page.locator("[data-loong-hero]").isVisible().catch(() => false);
  if (!heroOn) await page.waitForTimeout(180);
}
check("收束高潮祥龙本尊现身", heroOn);
await page.screenshot({ path: `${OUT}/a2b-hero.png` });
/* 跳过按钮可能在演出自然结束的瞬间卸载（竞态），点击失败则等自然收尾 */
await page.getByRole("button", { name: /跳过|Skip/ }).click({ timeout: 2500 }).catch(() => {});
await page.getByText(/七星既聚|Seven Stars Aligned/).first().waitFor({ state: "visible", timeout: 8000 });
check("演出收尾后祈愿卡弹出", true);
await page.screenshot({ path: `${OUT}/a2-ceremony.png` });

/* 愿·机缘 → 兑换码 + TG 深链 */
await page.getByRole("button", { name: /愿 · 机缘|Wish · Fortune/ }).click();
await page.waitForTimeout(1200);
const codeText = await page.locator("code").filter({ hasText: /^LOONG-/ }).first().textContent().catch(() => null);
check("兑换码签发", !!codeText && /^LOONG-[A-Z0-9]{6}$/.test(codeText ?? ""), codeText ?? "none");
const tgHref = await page.locator("a.dragon-tg-link").first().getAttribute("href").catch(() => null);
check("TG 深链带码", !!tgHref && tgHref.includes(`start=loong_${codeText}`), tgHref ?? "none");
const perfectNote = await page.getByText(/北斗正位|Perfect Dipper/).first().isVisible().catch(() => false);
check("正位加成提示（送皮肤）", perfectNote);
await page.screenshot({ path: `${OUT}/a3-code.png` });

/* 正位加成解锁皮肤 → 一键换装祥龙 */
const wearBtn = page.getByRole("button", { name: /切换祥龙形态|Wear the Loong/ });
check("换装入口出现", await wearBtn.isVisible().catch(() => false));
await wearBtn.click().catch(() => {});
await page.locator("button[aria-label='关闭'], button[aria-label='Close']").last().click().catch(() => {});
await page.waitForTimeout(1100); // 等遮罩退场 + 皮肤切换完成
await page.screenshot({ path: `${OUT}/a4-loong-skin.png`, clip: { x: 1440 - 460, y: 900 - 560, width: 460, height: 560 } });

/* ── A2. 月令龙鳞：三个月三轮 → 三鳞 → 兑「界龙之约」 ── */
const st1 = (await (await ctx.request.get(`${BASE}/api/dragon`)).json()).state;
check("完成首轮得 1 枚月鳞", st1.scales === 1, `scales=${st1.scales}`);
for (const [cycle, offset] of [[2, 32], [3, 64]]) {
  for (let d = 0; d < 7; d++) {
    await ctx.request.post(`${BASE}/api/dragon`, { data: { action: "collect" }, headers: { "x-dragon-now": String(t0 + (offset + d) * DAY) } });
  }
  await ctx.request.post(`${BASE}/api/dragon`, { data: { action: "wish", kind: cycle === 2 ? "skin" : "gift" }, headers: { "x-dragon-now": String(t0 + (offset + 6) * DAY) } });
}
const st3 = (await (await ctx.request.get(`${BASE}/api/dragon`, { headers: { "x-dragon-now": String(t0 + 70 * DAY) } })).json()).state;
check("跨三月三轮集齐 3 鳞", st3.scales === 3 && st3.grandReady === true, `scales=${st3.scales}`);
const grand = await (await ctx.request.post(`${BASE}/api/dragon`, { data: { action: "grand" }, headers: { "x-dragon-now": String(t0 + 70 * DAY) } })).json();
check("兑换界龙之约签发大奖码", grand.ok === true && /^LOONG-[A-Z0-9]{6}$/.test(grand.code ?? "") && grand.state.scales === 0, grand.code ?? "none");
const grand2 = await (await ctx.request.post(`${BASE}/api/dragon`, { data: { action: "grand" }, headers: { "x-dragon-now": String(t0 + 70 * DAY) } })).json();
check("鳞不足拒绝重复兑换", grand2.ok === false && grand2.reason === "not_ready");
await ctx.close();

/* ────────── B. API 边界：幂等 / 未集齐拒愿 / 皮肤愿 / 新周期 ────────── */
const ctx2 = await browser.newContext();
const api = ctx2.request;
const t1 = Date.now();

const early = await (await api.post(`${BASE}/api/dragon`, { data: { action: "wish", kind: "skin" } })).json();
check("未集齐拒绝许愿", early.ok === false && early.reason === "not_ready");

for (let d = 0; d < 7; d++) {
  await api.post(`${BASE}/api/dragon`, { data: { action: "collect" }, headers: { "x-dragon-now": String(t1 + d * DAY) } });
}
const dup = await (await api.post(`${BASE}/api/dragon`, { data: { action: "collect" }, headers: { "x-dragon-now": String(t1 + 6 * DAY) } })).json();
check("同日重复收珠幂等", dup.ok === true && dup.already === true);

const wishSkin = await (await api.post(`${BASE}/api/dragon`, { data: { action: "wish", kind: "skin" }, headers: { "x-dragon-now": String(t1 + 6 * DAY) } })).json();
check("愿·龙鳞解锁皮肤", wishSkin.ok === true && wishSkin.state.loongSkin === true && !wishSkin.state.code);

const rewish = await (await api.post(`${BASE}/api/dragon`, { data: { action: "wish", kind: "gift" }, headers: { "x-dragon-now": String(t1 + 6 * DAY) } })).json();
check("本轮重复许愿被拒", rewish.ok === false && rewish.reason === "already_wished");

const newCycle = await (await api.post(`${BASE}/api/dragon`, { data: { action: "collect" }, headers: { "x-dragon-now": String(t1 + 7 * DAY) } })).json();
check("愿后次日开新一轮", newCycle.ok === true && newCycle.index === 1 && newCycle.state.collected === 1);
await ctx2.close();

/* ────────── B2. 分享助力：令牌下发 / 好友代点 / 自助拦截 / 周期限额 ────────── */
const ctxD = await browser.newContext();
const dGet = await (await ctxD.request.get(`${BASE}/api/dragon`)).json();
check("分享令牌与 TG 载荷下发", typeof dGet.share === "string" && dGet.share.includes(".") && /^xz_[0-9a-f-]{50,}$/i.test(dGet.tg ?? ""), dGet.tg ?? "none");

const ctxE = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "dark" });
const pE = await ctxE.newPage();
pE.on("pageerror", (e) => errors.push("E: " + e));
await pE.addInitScript(() => {
  sessionStorage.setItem("bl-intro-seen", "1");
  sessionStorage.setItem("bl-sprite-greeted", "1");
  sessionStorage.setItem("yt-teaser", "1");
  sessionStorage.removeItem("bl-dragon-xz");
  sessionStorage.removeItem("bl-dragon-xz-done");
  sessionStorage.removeItem("bl-dragon-guide");
});
await pE.goto(`${BASE}/?xz=${encodeURIComponent(dGet.share)}`, { waitUntil: "domcontentloaded", timeout: 90000 });
/* dev 环境偶发 Fast Refresh 整页刷新会毁掉 evaluate 上下文——轮询容错（done 标记保证不会重复核销） */
let xzCleaned = false;
for (let i = 0; i < 16 && !xzCleaned; i++) {
  try {
    xzCleaned = await pE.evaluate(() => !location.search.includes("xz="));
  } catch {
    /* navigation in flight */
  }
  if (!xzCleaned) await pE.waitForTimeout(400);
}
check("助力后 URL 参数已清理", xzCleaned);
const dSt1 = await (await ctxD.request.get(`${BASE}/api/dragon`)).json();
check("好友代点分享者今日星珠", dSt1.state.collected === 1, `collected=${dSt1.state.collected}`);
/* 双向奖励：帮点者自己的今日珠也自动入袋 */
const eSt1 = await (await ctxE.request.get(`${BASE}/api/dragon`)).json();
check("双向奖励：助力者今日珠入袋", eSt1.state.collected === 1 && eSt1.state.todayCollected === true, `helper collected=${eSt1.state.collected}`);

const selfR = await (await ctxD.request.post(`${BASE}/api/dragon`, { data: { action: "assist", token: dGet.share } })).json();
check("不能助力自己", selfR.ok === false && selfR.reason === "self");

const tA = Date.now();
const a2 = await (await ctxE.request.post(`${BASE}/api/dragon`, { data: { action: "assist", token: dGet.share }, headers: { "x-dragon-now": String(tA + DAY) } })).json();
const a3 = await (await ctxE.request.post(`${BASE}/api/dragon`, { data: { action: "assist", token: dGet.share }, headers: { "x-dragon-now": String(tA + 2 * DAY) } })).json();
check("助力限额 2 次/周期", a2.ok === true && a3.ok === false && a3.reason === "cap", `a3=${JSON.stringify(a3).slice(0, 80)}`);

/* 助力者进度自动刷新（今日珠已入袋 → 气泡隐藏、托盘直接可见），面板应有分享/TG 入口 */
await pE.locator(".dragon-tray").click().catch(() => {});
await pE.waitForTimeout(500);
check("面板含助力/TG 同步入口", (await pE.locator(".dragon-share-btn").isVisible().catch(() => false)) && (await pE.locator("a[href*='?start=xz_']").isVisible().catch(() => false)));
await pE.screenshot({ path: `${OUT}/b2-panel.png`, clip: { x: 1440 - 520, y: 900 - 620, width: 520, height: 620 } });
await ctxD.close();
await ctxE.close();

/* ────────── B3. 图鉴页 /loong：形态展厅 / 解锁态 / 穿戴写偏好 ────────── */
const ctxF = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "dark" });
const pF = await ctxF.newPage();
pF.on("pageerror", (e) => errors.push("F: " + e));
const tF = Date.now();
/* 先把该访客推到 loong 已解锁（7 连珠 + 愿·龙鳞） */
for (let d = 0; d < 7; d++) {
  await ctxF.request.post(`${BASE}/api/dragon`, { data: { action: "collect" }, headers: { "x-dragon-now": String(tF + d * DAY) } });
}
await ctxF.request.post(`${BASE}/api/dragon`, { data: { action: "wish", kind: "skin" }, headers: { "x-dragon-now": String(tF + 6 * DAY) } });
await pF.goto(`${BASE}/loong`, { waitUntil: "domcontentloaded", timeout: 90000 });
await pF.waitForSelector(".codex-form", { timeout: 30000 });
/* 手动轮询等 loong 卡点亮（状态 fetch 是挂载后异步返回的） */
let formStates = "";
for (let i = 0; i < 50; i++) {
  formStates = await pF.evaluate(() =>
    [...document.querySelectorAll(".codex-form")].map((el) => `${el.getAttribute("data-skin")}:${el.getAttribute("data-unlocked")}`).join(",")
  );
  if (formStates === "normal:1,demon:0,loong:1") break;
  await pF.waitForTimeout(400);
}
check("图鉴三形态与解锁态", formStates === "normal:1,demon:0,loong:1", formStates);
await pF.screenshot({ path: `${OUT}/f1-codex.png`, fullPage: true });
const wearBtn2 = pF.locator(".codex-wear-btn");
await wearBtn2.click();
const prefAfter = await pF.evaluate(() => localStorage.getItem("bl-skin-pref"));
check("图鉴穿戴写入偏好", prefAfter === "loong", `pref=${prefAfter}`);
await ctxF.close();

/* ────────── B4. 祥龙游动帧差：飞行态躯干 path d 跨帧变化（锥形分段骨架存活） ────────── */
const ctxSwim = await browser.newContext({ viewport: { width: 900, height: 900 }, colorScheme: "dark" });
const pSwim = await ctxSwim.newPage();
pSwim.on("pageerror", (e) => errors.push("Swim: " + e));
await pSwim.goto(`${BASE}/robot-stage?skin=loong&mode=flying&bg=ink&scale=3&pool=0`, {
  waitUntil: "domcontentloaded",
  timeout: 90000,
});
await pSwim.waitForSelector("[data-stage-ready]", { timeout: 60000 });
await pSwim.waitForTimeout(800);
const swimDiff = await pSwim.evaluate(async () => {
  const sample = () => {
    const paths = [...document.querySelectorAll("svg path[stroke-linecap='round']")];
    // 取最长几条描边 path 的 d（躯干主层），拼成签名
    return paths
      .map((p) => p.getAttribute("d") || "")
      .filter((d) => d.length > 40)
      .sort((a, b) => b.length - a.length)
      .slice(0, 6)
      .join("|");
  };
  const a = sample();
  await new Promise((r) => setTimeout(r, 280));
  const b = sample();
  await new Promise((r) => setTimeout(r, 280));
  const c = sample();
  let changed = 0;
  if (a && a !== b) changed++;
  if (b && b !== c) changed++;
  if (a && a !== c) changed++;
  return { changed, lenA: a.length, lenB: b.length };
});
check("祥龙飞行游动跨帧变化", swimDiff.changed >= 2, JSON.stringify(swimDiff));
await pSwim.screenshot({ path: `${OUT}/b4-loong-swim.png` });
await ctxSwim.close();

/* ────────── B5. 互动坞：聊天互斥 / 龙珠收纳 / 迷你星珠条收珠 / 恢复 ────────── */
const ctxDock = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "dark" });
const pD = await ctxDock.newPage();
pD.on("pageerror", (e) => errors.push("Dock: " + e));
await pD.addInitScript(() => {
  sessionStorage.setItem("bl-intro-seen", "1");
  sessionStorage.setItem("bl-sprite-greeted", "1");
  sessionStorage.setItem("yt-teaser", "1");
  sessionStorage.setItem("bl-dragon-guide", "1");
});
await pD.goto(BASE, { waitUntil: "domcontentloaded", timeout: 90000 });
await pD.waitForSelector(".dragon-pearl-offer", { timeout: 45000 });

/* 静止态零重叠：FAB vs 星珠气泡 */
const boxOf = async (sel) => pD.locator(sel).boundingBox().catch(() => null);
const intersects = (a, b) =>
  !!a && !!b && a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
const fabBox = await boxOf("button[aria-label='AI chat']");
const offerBox = await boxOf(".dragon-pearl-offer");
check("静止态 FAB 与星珠零重叠", !!fabBox && !!offerBox && !intersects(fabBox, offerBox));

/* 聊天打开 → 龙珠簇收纳 + 精灵让位 + 迷你星珠条接管
   （收纳=父簇 opacity 过渡到 0，Playwright isVisible 不识别 opacity——用 computed style 轮询） */
const opacityOf = (sel) =>
  pD
    .evaluate((s) => {
      const el = document.querySelector(s);
      return el ? Number(getComputedStyle(el).opacity) : -1;
    }, sel)
    .catch(() => -1);
const pollUntil = async (fn, ok, tries = 20, gap = 200) => {
  let v;
  for (let i = 0; i < tries; i++) {
    v = await fn();
    if (ok(v)) return { ok: true, v };
    await pD.waitForTimeout(gap);
  }
  return { ok: false, v };
};
const CLUSTER = ".dragon-pearl-offer, .dragon-tray";

await pD.locator("button[aria-label='AI chat']").click();
await pD.waitForSelector(".chat-pearl-bar", { timeout: 8000 }).catch(() => {});
/* 收纳形态有二：叫号机卸载（元素消失）或父簇 opacity→0，两者都算达标 */
const clusterFade = await pollUntil(
  () => pD.evaluate(() => {
    const offer = document.querySelector(".dragon-pearl-offer");
    if (!offer) return 0;
    const el = offer.closest("div[class*='fixed']");
    return el ? Number(getComputedStyle(el).opacity) : -1;
  }).catch(() => -1),
  (v) => v >= 0 && v < 0.05
);
check("聊天打开后星珠气泡收纳", clusterFade.ok, `opacity=${clusterFade.v}`);
const spriteFade = await pollUntil(() => opacityOf(".ai-sprite-container"), (v) => v >= 0 && v < 0.05);
check("聊天打开后精灵让位", spriteFade.ok, `opacity=${spriteFade.v}`);
const barVisible = await pD.locator(".chat-pearl-bar").isVisible().catch(() => false);
check("聊天头部迷你星珠条出现", barVisible);

/* 迷你条收珠：不离开聊天完成收珠，toast 底部居中可见 */
await pD.locator(".chat-pearl-collect").click().catch(() => {});
const toastSeen = await pD
  .locator(".dragon-toast")
  .waitFor({ state: "visible", timeout: 6000 })
  .then(() => true)
  .catch(() => false);
check("迷你条收珠 toast 底部居中", toastSeen);
await pD.waitForTimeout(400);
const barAfter = await pD.locator(".chat-pearl-bar").textContent().catch(() => "");
check("迷你条进度已更新", /1\s*\/\s*7/.test(barAfter ?? ""), (barAfter ?? "").trim().slice(0, 40));
await pD.screenshot({ path: `${OUT}/b5-dock-chat.png`, clip: { x: 1440 - 520, y: 900 - 640, width: 520, height: 640 } });

/* 关闭聊天 → 龙珠簇恢复（今日已收 → 气泡不回，托盘回） */
await pD.locator("button[aria-label='close']").click();
const spriteBack = await pollUntil(() => opacityOf(".ai-sprite-container"), (v) => v > 0.9);
const trayBack = await pollUntil(
  () => pD.locator(".dragon-tray").isVisible().catch(() => false),
  (v) => v === true
);
check("关闭聊天后托盘与精灵恢复", trayBack.ok && spriteBack.ok, `tray=${trayBack.v} sprite=${spriteBack.v}`);
await ctxDock.close();

/* ────────── B6. 叫号机：星珠 ↔ AI 招呼错峰（不预置 yt-teaser，走真实时序） ──────────
   时间线：t=0 星珠上台 → t=15s 星珠持有到期 → t=18s 招呼入队即轮转上台（星珠回队尾）
   → 关掉招呼 → 星珠回归。全程同屏自动气泡 ≤1。 */
const ctxAttn = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "dark" });
const pA = await ctxAttn.newPage();
pA.on("pageerror", (e) => errors.push("Attn: " + e));
await pA.addInitScript(() => {
  sessionStorage.setItem("bl-intro-seen", "1");
  sessionStorage.setItem("bl-sprite-greeted", "1");
  sessionStorage.setItem("bl-dragon-guide", "1");
  /* 故意不设 yt-teaser：让 AI 招呼在 18s 后真实弹出 */
});
await pA.goto(BASE, { waitUntil: "domcontentloaded", timeout: 90000 });
await pA.waitForSelector(".dragon-pearl-offer", { timeout: 45000 });
const teaserEarly = await pA.locator(".ai-teaser-bubble").isVisible().catch(() => false);
check("t<15s 星珠上台且招呼未弹", !teaserEarly);

/* 等招呼弹出（18s dwell + 轮转），轮询至 26s */
let teaserOn = false;
for (let i = 0; i < 60 && !teaserOn; i++) {
  teaserOn = await pA.locator(".ai-teaser-bubble").isVisible().catch(() => false);
  if (!teaserOn) await pA.waitForTimeout(400);
}
/* 星珠让位有 350ms exit 动画——轮询等它退场 */
let pearlHidden = false;
for (let i = 0; i < 12 && !pearlHidden; i++) {
  pearlHidden = !(await pA.locator(".dragon-pearl-offer").isVisible().catch(() => false));
  if (!pearlHidden) await pA.waitForTimeout(250);
}
check("招呼上台时星珠已让位（同屏≤1）", teaserOn && pearlHidden, `teaser=${teaserOn} pearlHidden=${pearlHidden}`);
await pA.screenshot({ path: `${OUT}/b6-attn-teaser.png`, clip: { x: 1440 - 520, y: 900 - 620, width: 520, height: 620 } });

/* 关闭招呼 → 星珠轮转回归（两侧动画都轮询等） */
await pA.locator(".ai-teaser-bubble button[aria-label='dismiss']").click().catch(() => {});
let pearlBack = false;
for (let i = 0; i < 20 && !pearlBack; i++) {
  pearlBack = await pA.locator(".dragon-pearl-offer").isVisible().catch(() => false);
  if (!pearlBack) await pA.waitForTimeout(300);
}
let teaserGone = false;
for (let i = 0; i < 12 && !teaserGone; i++) {
  teaserGone = !(await pA.locator(".ai-teaser-bubble").isVisible().catch(() => false));
  if (!teaserGone) await pA.waitForTimeout(250);
}
check("关招呼后星珠回归", pearlBack && teaserGone, `pearlBack=${pearlBack} teaserGone=${teaserGone}`);
await ctxAttn.close();

/* ────────── C. 伪造 cookie 防线 + 移动端可见性 ────────── */
const ctx3 = await browser.newContext();
await ctx3.addCookies([{ name: "bl_vid", value: "12345678-aaaa-bbbb-cccc-123456789012.deadbeefdeadbeef", url: BASE }]);
const forged = await (await ctx3.request.get(`${BASE}/api/dragon`)).json();
check("伪造签名视为新访客", forged.ok === true && forged.state.collected === 0);
await ctx3.close();

const mctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, colorScheme: "dark" });
const mpage = await mctx.newPage();
mpage.on("pageerror", (e) => errors.push("C: " + e));
await mpage.addInitScript(() => {
  sessionStorage.setItem("bl-intro-seen", "1");
  sessionStorage.setItem("bl-sprite-greeted", "1");
  sessionStorage.setItem("yt-teaser", "1");
});
await mpage.goto(BASE, { waitUntil: "domcontentloaded", timeout: 90000 });
await mpage.waitForSelector(".dragon-pearl-offer", { timeout: 20000 });
const mBox = await mpage.locator(".dragon-pearl-offer").boundingBox();
check("移动端星珠可见且不越界", !!mBox && mBox.x >= 0 && mBox.x + mBox.width <= 390, JSON.stringify(mBox));
await mpage.screenshot({ path: `${OUT}/c1-mobile.png` });
await mctx.close();

check("全流程无页面错误", errors.length === 0, errors.join(" | ").slice(0, 200));

await browser.close();
console.log(`\n${failCount === 0 ? "all checks passed" : failCount + " CHECKS FAILED"} (${passCount}/${passCount + failCount}) -> ${OUT}`);
process.exit(failCount === 0 ? 0 : 1);
