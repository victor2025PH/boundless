// 九产品的「展示元数据」单一来源：图标路径 + 首页内锚点。
// 纯文案/名称在 lib/brand.ts；这里只补 UI 层需要、又不该污染纯数据源的部分。
// ProductMatrix / /brand 页 / 小程序首页共用本文件，避免「同一映射散落多份、改一处漏一处」。
import type { ProductKey } from "@/lib/brand";

// 产品专属玻璃 3D 图标（透明底 256×256）：唯一来源是 brand-assets/ 新管线
// （build_brand_assets.py → 02_product-icons/{key}/{key}-256.png）。
// 公司 ∞ 主标同样由 brand-assets 管线同步（旧 scripts/build-boundless-marks.ps1 已于 2026-07-25 退役）。
export const PRODUCT_IMG: Record<ProductKey, string> = {
  reachx: "/brand/products/reachx.png",
  chatx: "/brand/products/chatx.png",
  facex: "/brand/products/facex.png",
  voicex: "/brand/products/voicex.png",
  livex: "/brand/products/livex.png",
  lingox: "/brand/products/lingox.png",
  voxx: "/brand/products/voxx.png",
  matrixx: "/brand/products/matrixx.png",
  fatex: "/brand/products/fatex.png",
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
  matrixx: "/matrix",
  fatex: "#showcase",
};

// 九款图标资产虽已按同一 bbox 归一（pad 8%），但形状密度差异很大：实心方块阵（智控）
// 同 bbox 下感观远大于稀疏麦克风（幻声）。光学补偿系数的单一真相是
// platform/brand/optical-scale.json（经 sync:brand → vendor/brand/optical-scale.json）。
//
// applyInUi=false：系数已烘焙进 PNG（brand-assets → public/brand/products），UI 不再二次缩放。
// applyInUi=true：临时在 ProductIcon / IntroCover 做 CSS scale（改系数后尚未重烘焙时用）。
import opticalScale from "@/vendor/brand/optical-scale.json";

type OpticalScaleFile = {
  applyInUi?: boolean;
  scales?: Partial<Record<ProductKey, number>>;
};

const OPTICAL = opticalScale as OpticalScaleFile;

export const PRODUCT_OPTICAL_SCALE: Partial<Record<ProductKey, number>> = OPTICAL.applyInUi
  ? { ...(OPTICAL.scales ?? {}) }
  : {};

// 每款图标的主色（"r,g,b" 字符串，取自玻璃 3D 图标的视觉主导色），
// 供辉光 drop-shadow / 拖尾光迹 / 出生涟漪等 UI 效果按产品配色，提升识别度。
export const PRODUCT_GLOW: Record<ProductKey, string> = {
  reachx: "251,146,60", // 靶心+飞镖：橙
  chatx: "217,70,239", // 对话气泡+星芒：紫红
  facex: "96,165,250", // 面具：蓝
  voicex: "167,139,250", // 麦克风：紫
  livex: "232,121,249", // 播放键：品红
  lingox: "56,189,248", // 地球+环绕箭头：天蓝
  voxx: "249,115,22", // 耳机+话筒：橙红
  matrixx: "242,67,214", // 闪电：品紫渐变（取自图标像素主色相）
  fatex: "206,86,203", // 水晶球/命盘：紫粉（幻境系，取自图标像素主色相）
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
  matrixx: "/matrix",
};
