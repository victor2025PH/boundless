/**
 * 生成投放用来源码 + bot 深链 CSV：
 *   npx tsx scripts/chatx-ad-links.ts [--cats biz,mkt] [--creatives trans,voice] [--variants 3] > ads.csv
 * 默认：全部类目 × 全部素材 × 2 个版本。
 */
import { AD_CATEGORIES, AD_CREATIVES, adStartLink, buildAdSrc, type AdCategory, type AdCreative } from "../lib/chatx-ad-src";

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

const cats = (arg("cats")?.split(",") ?? Object.keys(AD_CATEGORIES)) as AdCategory[];
const creatives = (arg("creatives")?.split(",") ?? Object.keys(AD_CREATIVES)) as AdCreative[];
const variants = Number(arg("variants") ?? 2);

const csv = (v: string) => (/[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
const lines = ["src,category,category_zh,creative,creative_zh,variant,bot_link"];
for (const c of cats) {
  for (const k of creatives) {
    for (let n = 1; n <= variants; n++) {
      const src = buildAdSrc(c, k, n);
      lines.push([src, c, AD_CATEGORIES[c], k, AD_CREATIVES[k], String(n), adStartLink(src)].map(csv).join(","));
    }
  }
}
process.stdout.write("\uFEFF" + lines.join("\n") + "\n");
