// 七产品的「展示元数据」单一来源：图标路径 + 首页内锚点。
// 纯文案/名称在 lib/brand.ts；这里只补 UI 层需要、又不该污染纯数据源的部分。
// ProductMatrix / /brand 页 / 小程序首页共用本文件，避免「同一映射散落多份、改一处漏一处」。
import type { ProductKey } from "@/lib/brand";

// 产品专属玻璃 3D 图标（透明底 256×256）：唯一来源是 brand-assets/ 新管线
// （build_brand_assets.py → 02_product-icons/{key}/{key}-256.png）。
// 官网侧 scripts/build-boundless-marks.ps1 仅负责公司 ∞ 主标，不再产产品图标。
export const PRODUCT_IMG: Record<ProductKey, string> = {
  reachx: "/brand/products/reachx.png",
  chatx: "/brand/products/chatx.png",
  facex: "/brand/products/facex.png",
  voicex: "/brand/products/voicex.png",
  livex: "/brand/products/livex.png",
  lingox: "/brand/products/lingox.png",
  voxx: "/brand/products/voxx.png",
};

// 每个产品在首页跳转到的现有 demo / 详情 section（均为已存在的真实锚点，
// 见 SectionNav：autochat / realtime / showcase）。避免坏锚点。
export const PRODUCT_ANCHOR: Record<ProductKey, string> = {
  reachx: "#autochat",
  chatx: "#autochat",
  facex: "#showcase",
  voicex: "#realtime",
  livex: "#realtime",
  lingox: "#translate",
  voxx: "#realtime",
};

// 圆形/近圆轮廓在同等 bbox 下视觉偏小，展示时略放大做光学补偿（仅 UI，不改资产文件）。
export const PRODUCT_OPTICAL_SCALE: Partial<Record<ProductKey, number>> = {
  reachx: 1.06,
  lingox: 1.05,
  facex: 1.03,
};

// 拥有独立落地页的产品线（zh 路径；en 为 /en 前缀）。矩阵卡片优先跳落地页，
// 没有落地页的产品仍回退到首页锚点。
export const PRODUCT_LANDING: Partial<Record<ProductKey, string>> = {
  voicex: "/voice",
  // 幻境系共用 /face，hash 区分出片 / 开播（StudioDualPath 消费）
  facex: "/face#swap",
  livex: "/face#live",
  // 智连系共用 /growth，hash 区分获客 / 成交
  reachx: "/growth#reach",
  chatx: "/growth#chat",
  // 通达系共用 /interpreting，用 hash 区分双轨（LingoDualPath 消费）
  lingox: "/interpreting#chat",
  voxx: "/interpreting#interpret",
};
