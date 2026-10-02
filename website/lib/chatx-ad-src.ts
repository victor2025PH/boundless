/**
 * 广告来源码：ad_<频道类目>_<素材><两位序号>，例如 ad_biz_voice01。
 * 一个 Telegram Ads 广告（频道 × 素材）一个码，报表按码 / 类目 / 素材聚合。
 * Telegram start 参数只允许 [A-Za-z0-9_-]、≤64；bot 侧 SRC_RE 限 48。
 */
export const AD_CATEGORIES = {
  biz: "跨境 / 外贸 / 出海",
  mkt: "营销 / 私域 / 客服",
  ai: "AI 工具",
  tech: "科技 / 效率",
  crypto: "币圈 / Web3",
  cn: "华语综合",
} as const;

export const AD_CREATIVES = {
  trans: "实时翻译",
  reply: "AI 自动回复",
  voice: "语音 + 人设",
  clone: "克隆自己的声音",
  multi: "多账号聚合",
} as const;

export type AdCategory = keyof typeof AD_CATEGORIES;
export type AdCreative = keyof typeof AD_CREATIVES;

const AD_RE = /^ad_([a-z]+)_([a-z]+)(\d{2})$/;

export function buildAdSrc(cat: AdCategory, creative: AdCreative, n: number): string {
  if (!(cat in AD_CATEGORIES) || !(creative in AD_CREATIVES)) throw new Error(`unknown ad code part: ${cat}/${creative}`);
  if (!Number.isInteger(n) || n < 1 || n > 99) throw new Error(`ad variant must be 1–99: ${n}`);
  return `ad_${cat}_${creative}${String(n).padStart(2, "0")}`;
}

export function parseAdSrc(src: string): { cat: AdCategory; creative: AdCreative; n: number } | null {
  const m = AD_RE.exec(src);
  if (!m || !(m[1] in AD_CATEGORIES) || !(m[2] in AD_CREATIVES) || m[3] === "00") return null;
  return { cat: m[1] as AdCategory, creative: m[2] as AdCreative, n: Number(m[3]) };
}

export function adStartLink(src: string, handle = process.env.NEXT_PUBLIC_CHATX_BOT_HANDLE || "ctx2026_bot"): string {
  if (!parseAdSrc(src)) throw new Error(`not an ad src: ${src}`);
  return `https://t.me/${handle}?start=${src}`;
}
