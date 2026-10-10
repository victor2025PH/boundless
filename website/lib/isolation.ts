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

/** 高风险产品的独立注册域（与主站不同注册域、不同主体、互不链接）。
 *  2026-07-19 迁移：原 13x.lol → ai2026.co（历史记录见 docs/实施10_合规隔离_13xlol_*）。 */
export const ISOLATED_DOMAIN = "ai2026.co";

export const ISOLATED_SITE_URL = process.env.NEXT_PUBLIC_ISOLATED_URL || "https://ai2026.co";

export type Visibility = "public" | "gated";

/** 路由可见性映射。未登记的路由默认 public；此处只登记 gated（高风险）路由。
 *  未来其他高风险产品（如情感陪伴 "/companion"）上线时同样在此登记。
 *
 *  2026-07-20 第九阶段新增 "/matrix"（智控 MatrixX，原「智控王」）：不是 deepfake 类
 *  监管风险，而是"客户画像风险"——其独立引擎侧的既有市场材料把加密货币/博彩/成人
 *  产业列为目标付费客户（详见融合方案文档 §9.24），若在主站公开可搜索索引，会把这层
 *  客户画像风险传导给"无界科技"这个母品牌与主域名信誉。处理方式与 /face 完全对齐
 *  （仅 noindex + robots disallow，页面/导航/产品矩阵不隐藏，直达链接仍可访问）——
 *  这是当前代码库对 gated 路由的实际处理水平（见 docs/实施10），不是"给智控单独降低
 *  标准"，也不是"做到位"，只是与既有唯一先例保持一致；若未来风险评估认为需要更强隔离
 *  （比照 face 的独立域 ISOLATED_DOMAIN 方案），需要单独的运维/法务动作，不是本次改代码
 *  范围内能完成的事。 */
export const ROUTE_VISIBILITY: Record<string, Visibility> = {
  "/face": "gated",
  "/matrix": "gated",
  "/matrix/download": "gated",
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
