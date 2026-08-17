/**
 * 彩蛋/活动预告素材卡生成 v2 —— 「召唤现场」构图。
 *
 * 与 v1 的关键差异：
 * - 立绘活体截图：直接从 /robot-stage 截「当前版本」的角色（不再依赖 assets:robot 预产物），
 *   角色精修后重跑本脚本即得新图，素材永远不落后于产品；
 * - 场景化：北斗七星连线（与站内托盘同源坐标）+ 金尘粒子 + 三层光带，不再是纯黑排版卡；
 * - 域名从 NEXT_PUBLIC_SITE_URL 读取（默认 bd2026.cc），根治写死旧域名；
 * - 产物直接写 public/brand/campaign/（/loong 页 OG 引用同名文件，覆盖即生效）。
 *
 * 用法：npm run assets:teaser [-- egg|loong]（缺省两个都出）
 * 环境：ROBOT_BASE_URL 指定截立绘的站点（默认 http://localhost:3210，需 dev/prod 在跑）
 *       TEASER_BG 可选背景图路径（AI 氛围底图，C 方案），优先级高于 campaign 内置 bg
 *
 * 素材矩阵（战报/里程碑等运营图）：同一构图换文案，用 env 覆盖后指定单 campaign 运行——
 *   TEASER_TITLE="本周 <span class=\"hl\">12</span> 位旅人<br>召唤成功。"
 *   TEASER_PERKS="🐉 新增祥龙持有 9 人|🌟 收珠 340 颗|🔥 最长连击 21 天"（| 分隔）
 *   TEASER_HINT="*七星聚 · 每周战报"  TEASER_OUT=war  npm run assets:teaser -- loong
 * 产物写为 teaser-<TEASER_OUT>-*.png，不覆盖主 teaser。
 */
import { chromium } from "playwright";
import { readFile, mkdir, access } from "node:fs/promises";
import path from "node:path";
import sharp from "sharp";

const ROOT = path.resolve(process.cwd());
const OUT = path.join(ROOT, "public", "brand", "campaign");
const LOGO_PNG = path.join(ROOT, "public", "brand", "logos", "boundless-mark-256.png");
const BASE = process.env.ROBOT_BASE_URL || "http://localhost:3210";
const SITE = (process.env.NEXT_PUBLIC_SITE_URL || "https://bd2026.cc").replace(/^https?:\/\//, "").replace(/\/$/, "");

/** 北斗七星（与 DragonQuest/LoongCodex 同源坐标，viewBox 100×64） */
const DIPPER = [
  [14, 14], [10, 36], [28, 46], [36, 22], [56, 30], [73, 40], [90, 52],
];
const DIPPER_PATH = "M36 22 L14 14 L10 36 L28 46 L36 22 L56 30 L73 40 L90 52";

const CAMPAIGNS = {
  egg: {
    stage: "skin=demon&mode=idle_wave",
    settle: 1800,
    accent: "#f43f5e",
    eyebrow: "官网彩蛋 EASTER EGG",
    title: `连点 <span class="hl">7</span> 次，<br>有惊喜。`,
    perks: ["🦇 隐藏恶魔形态", "🩸 蝠群化影飞行", "↩️ 一键还原"],
    hint: "*纯视觉彩蛋 · 可一键还原",
    glow: "rgba(190,30,60,0.20)",
    glow2: "rgba(120,20,80,0.14)",
    dipper: false,
    bg: path.join(ROOT, "public", "brand", "campaign", "bg-egg-night.png"),
  },
  loong: {
    stage: "skin=loong&mode=flying",
    settle: 1100,
    accent: "#f5c542",
    eyebrow: "七星聚 · 龙行无界 SEVEN STARS",
    title: `每天来一次，<br><span class="hl">7</span> 天召唤神龙。`,
    perks: ["🗝️ 体验月卡", "🐉 永久祥龙形态", "🎁 机缘礼包"],
    hint: "*断签不清零 · 每周期一愿",
    glow: "rgba(240,180,60,0.22)",
    glow2: "rgba(90,50,170,0.16)",
    dipper: true,
    /* C 方案：AI 祥云氛围底（一次性资产），存在即用；TEASER_BG env 可覆盖 */
    bg: path.join(ROOT, "public", "brand", "campaign", "bg-loong-clouds.png"),
  },
};

const pick = (process.argv[2] || "").trim();
const jobs = pick && CAMPAIGNS[pick] ? [pick] : Object.keys(CAMPAIGNS);

/* 文案 env 覆盖（素材矩阵）：仅单 campaign 运行时生效，产物名用 TEASER_OUT 区分 */
let outName = null;
if (jobs.length === 1) {
  const c = CAMPAIGNS[jobs[0]];
  if (process.env.TEASER_TITLE) c.title = process.env.TEASER_TITLE;
  if (process.env.TEASER_PERKS) c.perks = process.env.TEASER_PERKS.split("|").map((s) => s.trim()).filter(Boolean);
  if (process.env.TEASER_HINT) c.hint = process.env.TEASER_HINT;
  if (process.env.TEASER_EYEBROW) c.eyebrow = process.env.TEASER_EYEBROW;
  if (process.env.TEASER_OUT) outName = process.env.TEASER_OUT.replace(/[^a-z0-9-]/gi, "");
}

const logoSrc = `data:image/png;base64,${(await readFile(LOGO_PNG)).toString("base64")}`;

/** 背景解析：TEASER_BG env > campaign.bg（存在即用）> 程序化渐变。删除 bg 文件即回纯程序化。 */
async function resolveBg(c) {
  for (const cand of [process.env.TEASER_BG, c.bg].filter(Boolean)) {
    try {
      await access(cand);
      return `data:image/png;base64,${(await readFile(cand)).toString("base64")}`;
    } catch {
      /* try next */
    }
  }
  return "";
}

/* 星点（白，细）+ 金尘（金，模糊微光）：黄金角伪随机，重复生成像素级一致 */
const stars = Array.from({ length: 110 }, (_, i) => {
  const x = ((i * 137.5) % 100).toFixed(2);
  const y = ((i * 61.8 + 23) % 100).toFixed(2);
  const s = (i % 3) * 0.5 + 0.5;
  const o = ((i % 5) + 2) / 15;
  return `<i style="left:${x}%;top:${y}%;width:${s}px;height:${s}px;opacity:${o}"></i>`;
}).join("");
const dust = (accent) =>
  Array.from({ length: 26 }, (_, i) => {
    const x = ((i * 222.5 + 40) % 100).toFixed(2);
    const y = ((i * 96.4 + 55) % 100).toFixed(2);
    const s = (i % 4) + 2;
    const o = (((i * 7) % 5) + 2) / 12;
    const b = (i % 3) + 1;
    return `<b style="left:${x}%;top:${y}%;width:${s}px;height:${s}px;opacity:${o};filter:blur(${b}px);background:${accent}"></b>`;
  }).join("");

/** 北斗层：连线沿龙身后方展开，点亮 7 星 */
const dipperSvg = (accent, cls) => `
  <svg class="dipper ${cls}" viewBox="0 0 100 64" fill="none">
    <path d="${DIPPER_PATH}" stroke="${accent}" stroke-opacity="0.5" stroke-width="0.7" stroke-dasharray="2 2.4"/>
    ${DIPPER.map(
      ([x, y], i) => `
      <circle cx="${x}" cy="${y}" r="3.4" fill="${accent}" opacity="0.16"/>
      <circle cx="${x}" cy="${y}" r="${i === 6 ? 2 : 1.5}" fill="${accent}" opacity="0.95"/>`
    ).join("")}
  </svg>`;

const css = (c) => `
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: "Microsoft YaHei", "PingFang SC", sans-serif; background: #05060f; overflow: hidden; }
  .card { position: relative; width: 100vw; height: 100vh; overflow: hidden; background:
      ${c.bgSrc ? "#05060f" : `
      radial-gradient(58% 66% at 72% 60%, ${c.glow} 0%, transparent 68%),
      radial-gradient(46% 50% at 16% 16%, ${c.glow2} 0%, transparent 70%),
      radial-gradient(80% 34% at 50% 104%, ${c.glow.replace(/0\.\d+\)/, "0.30)")} 0%, transparent 72%),
      linear-gradient(168deg, #070812 0%, #05060f 52%, #0a0812 100%)`};
      display: flex; align-items: center; }
  .bgimg { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: 0.9; }
  .veil { position: absolute; inset: 0; background: linear-gradient(90deg, rgba(5,6,15,0.86) 0%, rgba(5,6,15,0.55) 44%, rgba(5,6,15,0.12) 100%); }
  .card i { position: absolute; border-radius: 50%; background: #fff; z-index: 1; }
  .card b { position: absolute; border-radius: 50%; z-index: 1; }
  .dipper { position: absolute; z-index: 2; filter: drop-shadow(0 0 6px ${c.accent}66); }
  .txt { position: relative; z-index: 4; }
  .eyebrow { display: flex; align-items: center; gap: 10px; color: #9aa3b5; letter-spacing: 4px; font-size: 15px; }
  .eyebrow img { height: 26px; }
  .eyebrow bb, .eyebrow .em { color: ${c.accent}; font-weight: 600; }
  h1 { color: #fff; font-weight: 800; line-height: 1.16; letter-spacing: 1px; }
  h1 .hl { color: ${c.accent}; text-shadow: 0 0 26px ${c.accent}99; }
  .perks { display: flex; flex-direction: column; gap: 10px; }
  .perk { display: inline-flex; align-items: center; gap: 10px; color: #d7dcea; font-weight: 500;
      text-shadow: 0 1px 8px rgba(0,0,0,0.6); }
  .perk::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: ${c.accent}; box-shadow: 0 0 7px ${c.accent}; }
  .site { display: inline-flex; align-items: center; gap: 9px; color: #0b0c14; font-weight: 700; background: linear-gradient(90deg, ${c.accent}, #ffe9a8);
      border-radius: 999px; box-shadow: 0 0 22px ${c.accent}55; }
  .hero { position: absolute; z-index: 3; filter: drop-shadow(0 22px 48px ${c.glow.replace(/0\.\d+\)/, "0.55)")}) drop-shadow(0 0 26px ${c.accent}55); }
  .halo { position: absolute; z-index: 2; border-radius: 50%;
      background: radial-gradient(circle, ${c.accent}3a 0%, ${c.accent}14 46%, transparent 70%); }
  .hint { position: absolute; z-index: 4; color: #6b7386; letter-spacing: 2px; }
`;

const layoutCss = {
  wide: `
  .txt { padding-left: 84px; max-width: 600px; }
  .eyebrow { margin-bottom: 26px; }
  h1 { font-size: 64px; margin-bottom: 26px; }
  .perks { margin-bottom: 36px; }
  .perk { font-size: 21px; }
  .site { font-size: 19px; padding: 11px 26px; }
  .hero { right: 130px; bottom: 20px; width: 375px; }
  .halo { right: 80px; bottom: -60px; width: 500px; height: 500px; }
  .dipper { right: 500px; top: 60px; width: 330px; opacity: 0.9; }
  .hint { left: 86px; bottom: 34px; font-size: 13px; }
  `,
  square: `
  .card { align-items: flex-start; }
  .txt { padding: 72px 64px 0; max-width: 100%; }
  .eyebrow { margin-bottom: 26px; }
  h1 { font-size: 76px; margin-bottom: 26px; }
  .perks { flex-direction: row; gap: 22px; margin-bottom: 30px; }
  .perk { font-size: 21px; }
  .site { font-size: 21px; padding: 12px 28px; }
  .hero { left: 46%; transform: translateX(-50%); bottom: 26px; width: 450px; }
  .halo { left: 46%; transform: translateX(-50%); bottom: -80px; width: 640px; height: 640px; }
  .dipper { right: 18px; top: 150px; width: 330px; opacity: 0.9; }
  .hint { right: 64px; top: 56px; font-size: 14px; }
  `,
};

const html = (w, h, layout, c) => `<!doctype html><html><head><meta charset="utf-8"><style>${css(c)}${layoutCss[layout]}</style></head><body>
  <div class="card" style="width:${w}px;height:${h}px">
    ${c.bgSrc ? `<img class="bgimg" src="${c.bgSrc}"><div class="veil"></div>` : ""}
    ${stars}${dust(c.accent)}
    ${c.dipper ? dipperSvg(c.accent, layout) : ""}
    <div class="halo"></div>
    <img class="hero" src="${c.heroSrc}" alt="">
    <div class="txt">
      <div class="eyebrow"><img src="${logoSrc}" alt=""><span>BOUNDLESS · <span class="em">${c.eyebrow}</span></span></div>
      <h1>${c.title}</h1>
      <div class="perks">${c.perks.map((p) => `<span class="perk">${p}</span>`).join("")}</div>
      <span class="site">${SITE}</span>
    </div>
    <div class="hint">${c.hint}</div>
  </div>
</body></html>`;

await mkdir(OUT, { recursive: true });
const browser = await chromium.launch();

/* ── 1. 活体截立绘：当前线上形态，透明背景 3x ── */
for (const key of jobs) {
  const c = CAMPAIGNS[key];
  c.bgSrc = await resolveBg(c);
  const page = await browser.newPage({ viewport: { width: 1000, height: 1100 }, colorScheme: "dark" });
  await page.goto(`${BASE}/robot-stage?${c.stage}&scale=3&bg=transparent&pool=0`, {
    waitUntil: "domcontentloaded",
    timeout: 90000,
  });
  await page.waitForSelector("[data-stage-ready]", { timeout: 60000 });
  await page.waitForTimeout(c.settle);
  const raw = await page.screenshot({ omitBackground: true });
  /* trim 裁掉透明边：角色顶格，合成时按真实身形占幅（否则立绘带大片空白显得小） */
  const buf = await sharp(raw).trim().png().toBuffer();
  c.heroSrc = `data:image/png;base64,${buf.toString("base64")}`;
  /* 副产物：立绘落盘（hero-<key>.png）——服务端战报图（lib/war-card）等无浏览器场景复用 */
  await sharp(buf).toFile(path.join(OUT, `hero-${key}.png`));
  await page.close();
  console.log(`hero: ${key} (${c.stage}) captured from ${BASE}`);
}

/* ── 2. 合成两尺寸 ── */
for (const key of jobs) {
  const c = CAMPAIGNS[key];
  const name = outName ?? key;
  for (const j of [
    { file: `teaser-${name}-1280x720.png`, w: 1280, h: 720, layout: "wide" },
    { file: `teaser-${name}-1080x1080.png`, w: 1080, h: 1080, layout: "square" },
  ]) {
    const page = await browser.newPage({ viewport: { width: j.w, height: j.h } });
    await page.setContent(html(j.w, j.h, j.layout, c), { waitUntil: "networkidle" });
    await page.screenshot({ path: path.join(OUT, j.file) });
    await page.close();
    console.log("teaser:", j.file);
  }
}
await browser.close();
console.log("done ->", OUT, "| site:", SITE);
