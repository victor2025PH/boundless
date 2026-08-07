// 九产品的「展示元数据」单一来源：图标路径 + 首页内锚点。
// 纯文案/名称在 lib/brand.ts；这里只补 UI 层需要、又不该污染纯数据源的部分。
// ProductMatrix / /brand 页 / 小程序首页共用本文件，避免「同一映射散落多份、改一处漏一处」。
import { PRODUCT_ORDER, productsInCategory, type CategoryKey, type ProductKey } from "@/lib/brand";
import { STUDIO_PAID_FROM, studioTier } from "@/lib/avatarhub-pricing";
import { autochatOffers } from "@/lib/pricing";

// 产品专属玻璃 3D 图标（透明底 256×256）：唯一来源是 brand-assets/ 新管线
// （build_brand_assets.py → 02_product-icons/{key}/{key}-256.png）。
// 公司 ∞ 主标同样由 brand-assets 管线同步（旧 scripts/build-boundless-marks.ps1 已于 2026-07-25 退役）。
import assetRev from "@/vendor/brand/asset-rev.json";

const ICON_BASE: Record<ProductKey, string> = {
  reachx: "/brand/products/reachx",
  chatx: "/brand/products/chatx",
  facex: "/brand/products/facex",
  voicex: "/brand/products/voicex",
  livex: "/brand/products/livex",
  voxx: "/brand/products/voxx",
  matrixx: "/brand/products/matrixx",
  fatex: "/brand/products/fatex",
};

// 图标重烘焙后内容变了但路径不变，而 /brand/* 带 1 天 max-age + 7 天
// stale-while-revalidate → 老访客最长一周看旧图。rev 由 bake 脚本按图标内容算，
// 拼成 `?v=` 让缓存在改图当天即失效。
const REV = (assetRev as { rev?: string }).rev ?? "";

const stamped = (ext: string) =>
  Object.fromEntries(
    (Object.keys(ICON_BASE) as ProductKey[]).map((k) => [
      k,
      REV ? `${ICON_BASE[k]}${ext}?v=${REV}` : `${ICON_BASE[k]}${ext}`,
    ]),
  ) as Record<ProductKey, string>;

export const PRODUCT_IMG = stamped(".png");
// WebP 变体（IntroCover 星门粒子走原生 <img> 时用）；勿在调用侧对 PRODUCT_IMG
// 做 `.replace(/\.png$/, ".webp")`——带 `?v=` 后正则锚不住结尾会静默失效。
export const PRODUCT_IMG_WEBP = stamped(".webp");

// 每个产品在首页跳转到的现有 demo / 详情 section（均为已存在的真实锚点；
// 2026-08-04 起 #pricing 随「私有定制」报价大表下线，仅剩 #products / #autochat / #translate / #contact）。避免坏锚点。
export const PRODUCT_ANCHOR: Record<ProductKey, string> = {
  reachx: "#autochat",
  chatx: "#autochat",
  facex: "#products",
  voicex: "#products",
  livex: "#products",
  voxx: "#products",
  matrixx: "#products",
  fatex: "#products",
};

/** 主站公开陈列过滤清单（导航下拉 / 首页产品矩阵 / 品牌页 / 落地页家族导航消费）：
 *  - facex / matrixx：合规隔离 gated 线（lib/isolation.ts），页面保留可直达，
 *    但不再从主站任何公开入口露出（风险画像不绑母品牌）；
 *  - fatex：有独立落地页 /fate（品牌层九磁贴与 sitemap 可达），但不进销售陈列位
 *    （矩阵/导航下拉）。
 *  gated 落地页自身不消费本清单，直达渲染不受影响。 */
export const PUBLIC_LIST_HIDDEN: ReadonlySet<ProductKey> = new Set(["facex", "matrixx", "fatex"]);

/** 某系下「主站公开可陈列」的产品（productsInCategory 的过滤包装）。 */
export function publicProductsInCategory(cat: CategoryKey): ProductKey[] {
  return productsInCategory(cat).filter((k) => !PUBLIC_LIST_HIDDEN.has(k));
}

/** 公开陈列顺序（编号 / 计数用；PRODUCT_ORDER 剔除隐藏线）。 */
export const PUBLIC_PRODUCT_ORDER: ProductKey[] = PRODUCT_ORDER.filter(
  (k) => !PUBLIC_LIST_HIDDEN.has(k),
);

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
  voxx: "249,115,22", // 耳机+话筒：橙红
  matrixx: "242,67,214", // 闪电：品紫渐变（取自图标像素主色相）
  fatex: "206,86,203", // 水晶球/命盘：紫粉（幻境系，取自图标像素主色相）
};

/* ── 产品卡价格锚（2026-08-07，修「拿着产品名找不到价格」）──
 * 首页矩阵卡片一句话告诉访客「这条线的价格从哪起、去哪看」。
 * 数字一律从定价单源派生（avatarhub-pricing.TIERS / pricing.autochatOffers），零手写；
 * 档位归属与 /order 页 CAPABILITY_TIERS 对照表同构（幻声/幻影→标准版、通传→专业版），
 * 刻意不写「会员 39 起」——39 是会员起价但不含直播换脸等能力，按能力所在档报数才诚实。
 * 邀请制/未挂牌线（reachx）只标交付方式不标数字。 */
const CHATX_FROM = Math.min(...autochatOffers.map((o) => Number(o.price)));

export const PRODUCT_PRICE_HINT: Partial<Record<ProductKey, { zh: string; en: string }>> = {
  chatx: { zh: `套餐 ${CHATX_FROM} USD/月起`, en: `Plans from $${CHATX_FROM}/mo` },
  reachx: { zh: "邀请制 · 评估后报价", en: "Invite-only · quoted" },
  voicex: {
    zh: `幻境 STUDIO 标准版 ${studioTier("standard").monthly} USD/月起（会员 ${STUDIO_PAID_FROM} 起）`,
    en: `STUDIO Standard from $${studioTier("standard").monthly}/mo (membership from $${STUDIO_PAID_FROM})`,
  },
  livex: {
    zh: `幻境 STUDIO 标准版 ${studioTier("standard").monthly} USD/月起（会员 ${STUDIO_PAID_FROM} 起）`,
    en: `STUDIO Standard from $${studioTier("standard").monthly}/mo (membership from $${STUDIO_PAID_FROM})`,
  },
  voxx: {
    zh: `幻境 STUDIO 专业版 ${studioTier("pro").monthly} USD/月起`,
    en: `STUDIO Pro from $${studioTier("pro").monthly}/mo`,
  },
};

// 拥有独立落地页的产品线（zh 路径；en 为 /en 前缀）。矩阵卡片优先跳落地页，
// 没有落地页的产品仍回退到首页锚点。
// 注意：gated 线刻意不登记（facex、livex 原共用幻颜落地页；matrixx 原智控落地页）——
// 这些页面仍可直达，但主站公开组件（含 BrandShowcase 等本文件消费方）
// 不得再渲染指向 gated 路由的链接（合规隔离，见 lib/isolation.ts）。
export const PRODUCT_LANDING: Partial<Record<ProductKey, string>> = {
  voicex: "/voice",
  // 智连系共用 /growth，hash 区分获客 / 成交
  reachx: "/growth#reach",
  chatx: "/growth#chat",
  // 通达系落地页 /interpreting：通传为主轨；聊天翻译已并入智聊（页内指路卡跳智聊，
  // 旧 #chat/#lingox 深链仍被 LingoDualPath 兼容）。
  voxx: "/interpreting#interpret",
  // 幻缘：独立落地页（BrandShowcase 磁贴由此拿链接）；仍在 PUBLIC_LIST_HIDDEN，
  // 不进矩阵/导航下拉等销售陈列位。
  fatex: "/fate",
};
