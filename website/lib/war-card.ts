import path from "path";
import { access } from "fs/promises";
import sharp from "sharp";

/**
 * 界龙周报战报图：服务端 sharp 合成（祥云底图 + 祥龙立绘 + SVG 文字层），
 * 零浏览器依赖——weekly-report cron 里直接生成，产物给频道战报草稿当配图。
 *
 * 资产（public/brand/campaign/，由 assets:teaser 管线产出）：
 * - bg-loong-clouds.png  AI 祥云氛围底（缺失则纯色底）
 * - hero-loong.png       当前版本祥龙立绘（transparent，trim 过）
 * 产物：teaser-war-latest.png（1080×1080，覆盖式——频道草稿引用固定路径）
 *
 * 中文渲染依赖系统字体（Noto Sans CJK 等）；服务器缺字体时文字会缺字形，
 * 部署侧需 fonts-noto-cjk（deploy 文档已注明）。
 */

const DIPPER: Array<[number, number]> = [
  [14, 14], [10, 36], [28, 46], [36, 22], [56, 30], [73, 40], [90, 52],
];
const DIPPER_LINKS: Array<[number, number]> = [
  [3, 0], [0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6],
];
const GOLD = "#f5c542";
const GOLD_HI = "#ffe9a8";

export interface WarCardStats {
  summons: number;
  newCollectors: number;
  pearls: number;
  skinOwners: number;
}

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

function textLayer(stats: WarCardStats, site: string): string {
  const W = 1080;
  const H = 1080;
  const scale = 3.4;
  const dx = W - 100 * scale - 56;
  const dy = 170;
  const P = DIPPER.map(([x, y]) => [dx + x * scale, dy + y * scale] as const);
  const font = `font-family="Noto Sans CJK SC, Noto Sans SC, WenQuanYi Zen Hei, Microsoft YaHei, sans-serif"`;

  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="veil" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#05060f" stop-opacity="0.88"/>
      <stop offset="55%" stop-color="#05060f" stop-opacity="0.45"/>
      <stop offset="100%" stop-color="#05060f" stop-opacity="0.10"/>
    </linearGradient>
    <linearGradient id="pill" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="${GOLD}"/>
      <stop offset="100%" stop-color="${GOLD_HI}"/>
    </linearGradient>
  </defs>
  <rect width="${W}" height="${H}" fill="url(#veil)"/>

  <!-- 北斗 -->
  <g stroke="${GOLD}" stroke-opacity="0.45" stroke-width="2.2" stroke-dasharray="8 11">
    ${DIPPER_LINKS.map(([a, b]) => `<line x1="${P[a][0]}" y1="${P[a][1]}" x2="${P[b][0]}" y2="${P[b][1]}"/>`).join("")}
  </g>
  ${P.map(([x, y]) => `<circle cx="${x}" cy="${y}" r="13" fill="${GOLD}" opacity="0.16"/><circle cx="${x}" cy="${y}" r="6" fill="${GOLD}" opacity="0.95"/>`).join("")}

  <!-- 品牌行 -->
  <text x="64" y="92" ${font} font-size="24" letter-spacing="6" fill="#9aa3b5">BOUNDLESS · <tspan fill="${GOLD}" font-weight="600">界龙战报 WEEKLY</tspan></text>

  <!-- 标题：有召唤突出召唤，零召唤周以新旅人为主标题（避免「0 位召唤成功」的尴尬） -->
  ${
    stats.summons > 0
      ? `<text x="60" y="212" ${font} font-size="88" font-weight="800" fill="#ffffff">本周 <tspan fill="${GOLD}" font-size="100">${stats.summons}</tspan> 位旅人</text>
  <text x="60" y="326" ${font} font-size="88" font-weight="800" fill="#ffffff">召唤成功。</text>`
      : `<text x="60" y="212" ${font} font-size="88" font-weight="800" fill="#ffffff">本周 <tspan fill="${GOLD}" font-size="100">${stats.newCollectors}</tspan> 位新旅人</text>
  <text x="60" y="326" ${font} font-size="88" font-weight="800" fill="#ffffff">开始点星。</text>`
  }

  <!-- 统计行（与主标题去重） -->
  <g ${font} font-size="30" fill="#d7dcea">
    <circle cx="70" cy="404" r="5" fill="${GOLD}"/>
    <text x="92" y="415">${
      stats.summons > 0
        ? `新旅人开始点星 <tspan fill="${GOLD_HI}" font-weight="700">${stats.newCollectors}</tspan> 人`
        : `七星集齐即可召唤界龙许愿`
    }</text>
    <circle cx="70" cy="462" r="5" fill="${GOLD}"/>
    <text x="92" y="473">本周收下星珠 <tspan fill="${GOLD_HI}" font-weight="700">${stats.pearls}</tspan> 颗</text>
    <circle cx="70" cy="520" r="5" fill="${GOLD}"/>
    <text x="92" y="531">祥龙金鳞持有 <tspan fill="${GOLD_HI}" font-weight="700">${stats.skinOwners}</tspan> 人</text>
  </g>

  <!-- 域名 pill -->
  <rect x="60" y="576" rx="34" ry="34" width="${site.length * 19 + 76}" height="66" fill="url(#pill)"/>
  <text x="${60 + (site.length * 19 + 76) / 2}" y="620" ${font} font-size="32" font-weight="700" fill="#0b0c14" text-anchor="middle">${esc(site)}</text>

  <!-- 角注 -->
  <text x="${W - 60}" y="${H - 44}" ${font} font-size="22" letter-spacing="3" fill="#6b7386" text-anchor="end">*七星聚 · 每周战报</text>
</svg>`;
}

/** 生成战报图（1080×1080 PNG buffer）。资产缺失时降级：无底图用纯色，无立绘只出文字卡。 */
export async function renderWarCard(stats: WarCardStats, siteHost: string): Promise<Buffer> {
  const dir = path.join(process.cwd(), "public", "brand", "campaign");
  const bgPath = path.join(dir, "bg-loong-clouds.png");
  const heroPath = path.join(dir, "hero-loong.png");

  const has = async (p: string) => access(p).then(() => true, () => false);

  const base = (await has(bgPath))
    ? sharp(bgPath).resize(1080, 1080, { fit: "cover", position: "east" })
    : sharp({ create: { width: 1080, height: 1080, channels: 4, background: "#07080f" } });

  const layers: sharp.OverlayOptions[] = [];
  if (await has(heroPath)) {
    const hero = await sharp(heroPath).resize({ height: 430 }).png().toBuffer();
    const meta = await sharp(hero).metadata();
    layers.push({ input: hero, left: Math.round(1080 - (meta.width ?? 320) - 96), top: 1080 - (meta.height ?? 430) - 60 });
  }
  layers.push({ input: Buffer.from(textLayer(stats, siteHost)), left: 0, top: 0 });

  return base.composite(layers).png().toBuffer();
}
