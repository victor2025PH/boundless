/**
 * 彩蛋/活动预告素材卡生成：把已导出的立绘（assets:robot 产物）与文案合成为可直接
 * 投放 TG 频道/朋友圈的成品图。每个 campaign 出 1280×720 横版 + 1080×1080 方版。
 *
 * 用法：npm run assets:teaser [-- egg|loong]（缺省两个都出；需先跑 npm run assets:robot）
 */
import { chromium } from "playwright";
import { readFile, access } from "node:fs/promises";
import path from "node:path";

const ROOT = path.resolve(process.cwd());
const ASSETS = path.join(ROOT, ".robot-assets");
const LOGO_PNG = path.join(ROOT, "public", "brand", "logos", "boundless-mark-256.png");

const CAMPAIGNS = {
  egg: {
    asset: path.join(ASSETS, "eve-demon-idle_wave@3x.png"),
    accent: "#f43f5e",
    eyebrow: `官网彩蛋 EASTER EGG`,
    title: `连点 <span class="hl">7</span> 次，<br>有惊喜。`,
    sub: `官网右下角的 AI 小机器人，藏着一个隐藏形态——<br>去把它点醒，看看会发生什么。`,
    hint: `*纯视觉彩蛋 · 可一键还原`,
    glow: "rgba(190,30,60,0.16)",
  },
  loong: {
    asset: path.join(ASSETS, "eve-loong-idle_wave@3x.png"),
    accent: "#f5c542",
    eyebrow: `七星聚 · 龙行无界 SEVEN STARS`,
    title: `每天来一次，<br><span class="hl">7</span> 天召唤神龙。`,
    sub: `每日到访点亮一颗北斗星珠，七星聚齐、界龙现世——<br>体验月卡 / 祥龙形态 / 机缘礼包，任你许愿。`,
    hint: `*断签不清零 · 每周期一愿`,
    glow: "rgba(240,180,60,0.14)",
  },
};

const pick = (process.argv[2] || "").trim();
const jobs = pick && CAMPAIGNS[pick] ? [pick] : Object.keys(CAMPAIGNS);

const logoB64 = (await readFile(LOGO_PNG)).toString("base64");
const logoSrc = `data:image/png;base64,${logoB64}`;

/** 星点背景：确定性伪随机（黄金角分布），重复生成像素级一致 */
const stars = Array.from({ length: 90 }, (_, i) => {
  const x = ((i * 137.5) % 100).toFixed(2);
  const y = ((i * 61.8 + 23) % 100).toFixed(2);
  const s = (i % 3) * 0.5 + 0.5;
  const o = ((i % 5) + 2) / 14;
  return `<i style="left:${x}%;top:${y}%;width:${s}px;height:${s}px;opacity:${o}"></i>`;
}).join("");

const css = (accent, glow) => `
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: "Microsoft YaHei", "PingFang SC", sans-serif; background: #05060f; overflow: hidden; }
  .card { position: relative; width: 100vw; height: 100vh; background:
      radial-gradient(52% 62% at 74% 58%, ${glow} 0%, transparent 70%),
      radial-gradient(40% 44% at 18% 20%, rgba(80,40,160,0.12) 0%, transparent 70%),
      #05060f; display: flex; align-items: center; }
  .card i { position: absolute; border-radius: 50%; background: #fff; }
  .txt { position: relative; z-index: 2; }
  .eyebrow { display: flex; align-items: center; gap: 10px; color: #9aa3b5; letter-spacing: 4px; font-size: 15px; }
  .eyebrow img { height: 26px; }
  .eyebrow b { color: ${accent}; font-weight: 600; }
  h1 { color: #fff; font-weight: 800; line-height: 1.18; }
  h1 .hl { color: ${accent}; text-shadow: 0 0 22px ${accent}88; }
  .sub { color: #b7bfd0; line-height: 1.7; }
  .site { display: inline-flex; align-items: center; gap: 8px; color: #e7ebf3; background: ${accent}1a;
      border: 1px solid ${accent}59; border-radius: 999px; }
  .site::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: ${accent}; box-shadow: 0 0 8px ${accent}; }
  .demon { position: absolute; z-index: 1; filter: drop-shadow(0 18px 42px ${glow.replace("0.16", "0.4").replace("0.14", "0.4")}); }
  .hint { position: absolute; color: #6b7386; letter-spacing: 2px; }
`;

const html = (w, h, layout, c) => `<!doctype html><html><head><meta charset="utf-8"><style>${css(c.accent, c.glow)}
  ${layout === "wide" ? `
  .txt { padding-left: 84px; max-width: 640px; }
  .eyebrow { margin-bottom: 26px; }
  h1 { font-size: 60px; margin-bottom: 22px; }
  .sub { font-size: 20px; margin-bottom: 34px; }
  .site { font-size: 18px; padding: 10px 22px; }
  .demon { right: 48px; bottom: -30px; width: 470px; }
  .hint { right: 84px; top: 44px; font-size: 13px; }
  ` : `
  .card { align-items: flex-start; }
  .txt { padding: 84px 72px 0; max-width: 100%; }
  .eyebrow { margin-bottom: 30px; }
  h1 { font-size: 70px; margin-bottom: 24px; }
  .sub { font-size: 23px; margin-bottom: 36px; }
  .site { font-size: 20px; padding: 12px 26px; }
  .demon { left: 50%; transform: translateX(-50%); bottom: -40px; width: 560px; }
  .hint { right: 72px; top: 52px; font-size: 14px; }
  `}
</style></head><body>
  <div class="card" style="width:${w}px;height:${h}px">
    ${stars}
    <div class="txt">
      <div class="eyebrow"><img src="${logoSrc}" alt=""><span>BOUNDLESS · <b>${c.eyebrow}</b></span></div>
      <h1>${c.title}</h1>
      <div class="sub">${c.sub}</div>
      <span class="site">usdt2026.cc</span>
    </div>
    <img class="demon" src="${c.assetSrc}" alt="">
    <div class="hint">${c.hint}</div>
  </div>
</body></html>`;

const browser = await chromium.launch();
for (const key of jobs) {
  const c = CAMPAIGNS[key];
  try {
    await access(c.asset);
  } catch {
    console.error(`跳过 ${key}：缺少立绘 ${path.basename(c.asset)}，先运行 npm run assets:robot`);
    continue;
  }
  c.assetSrc = `data:image/png;base64,${(await readFile(c.asset)).toString("base64")}`;
  for (const j of [
    { file: `teaser-${key}-1280x720.png`, w: 1280, h: 720, layout: "wide" },
    { file: `teaser-${key}-1080x1080.png`, w: 1080, h: 1080, layout: "square" },
  ]) {
    const page = await browser.newPage({ viewport: { width: j.w, height: j.h } });
    await page.setContent(html(j.w, j.h, j.layout, c), { waitUntil: "networkidle" });
    await page.screenshot({ path: path.join(ASSETS, j.file) });
    await page.close();
    console.log("teaser:", j.file);
  }
}
await browser.close();
console.log("done ->", ASSETS);
