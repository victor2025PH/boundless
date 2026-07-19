/**
 * 合规隔离单一真相（SEO / 索引层）。
 *
 * 背景：换脸/直播换脸（deepfake 类）属高风险业务，2026 年监管、支付通道与
 * 广告渠道对其审查显著收紧。合规主站 bd2026.cc 只保留低风险产品
 * （翻译/语音/数字人/同传等）的公开可索引面；高风险路由在此登记为 "gated"：
 *   - 从主站 sitemap.xml 与 IndexNow 推送中剔除（lib/seo.ts 的 publicPages/indexableUrls）；
 *   - 在 robots.txt 中 disallow（app/robots.ts，zh 与 /en 版本成对）；
 *   - 页面 metadata 加 noindex/nofollow（app/face/page.tsx、app/en/face/page.tsx）。
 * 页面本身保留，仅供直达/受控（gated）访问；对外经营面迁往独立隔离站
 * ISOLATED_DOMAIN —— 独立注册域 + 独立公司/收款主体，与主站不互链、不共享索引，
 * 详见 docs/实施09_合规隔离_13xlol_独立域与主体_2026-07.md。
 *
 * 本文件刻意零依赖（不 import brand.ts / site.ts 等热点文件），
 * middleware / app / lib 任何一层均可安全引用。
 */

/** 高风险产品的独立注册域（与主站不同注册域、不同主体、互不链接）。 */
export const ISOLATED_DOMAIN = "13x.lol";

export const ISOLATED_SITE_URL = process.env.NEXT_PUBLIC_ISOLATED_URL || "https://13x.lol";

export type Visibility = "public" | "gated";

/** 路由可见性映射。未登记的路由默认 public；此处只登记 gated（高风险）路由。
 *  未来其他高风险产品（如情感陪伴 "/companion"）上线时同样在此登记。 */
export const ROUTE_VISIBILITY: Record<string, Visibility> = {
  "/face": "gated",
};

/** gated slug 列表（由 ROUTE_VISIBILITY 派生，勿手工另行维护）。 */
export const GATED_SLUGS: string[] = Object.entries(ROUTE_VISIBILITY)
  .filter(([, visibility]) => visibility === "gated")
  .map(([slug]) => slug);

export function isGatedSlug(slug: string): boolean {
  return ROUTE_VISIBILITY[slug] === "gated";
}

export function isPublicSlug(slug: string): boolean {
  return !isGatedSlug(slug);
}
