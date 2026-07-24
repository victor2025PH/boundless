// /order 面板的多产品线适配层（2026-07-24 P7）：把 ChatX / LingoX 的服务类 SKU
// （lib/pricing.ts 单一价格真相）适配成 AvatarHub 面板同构的 Tier 卡片，让下单面板
// 一套交互三条产品线通吃。
//
// 设计约束：
//  - 价格绝不在本文件复写数字——一律按 offer id 从 pricing.ts 派生（改价只改 pricing.ts）；
//  - key = offer id（autochat-entry / translate-charpack …）＝ POST /api/order 的 plan 参数，
//    经 lib/offer-map.ts::resolveOrderSku 映射全域 SKU → 引擎自动履约；
//  - 年付沿用全站惯例 ×10（送 2 个月，lib/avatarhub-pricing.ANNUAL_MONTHS）；
//    一次性商品（charpack）标 oneTime，周期切换不影响其价格；
//  - 首年 8 折促销（FIRST_YEAR_PROMO）是 AvatarHub 专属，本适配层产品线不参与。
import { autochatOffers, translateOffers, type PriceOffer } from "@/lib/pricing";
import type { Tier } from "@/lib/avatarhub-pricing";

export type OrderFamily = "avatarhub" | "chatx" | "lingox";

/** Tier 超集：oneTime=一次性买断（周期切换不变价，结算页显示「一次性」）。 */
export interface LineTier extends Tier {
  oneTime?: boolean;
}

function priceOf(offers: PriceOffer[], id: string): number {
  const o = offers.find((x) => x.id === id);
  if (!o) throw new Error(`order-lines: offer not found: ${id}`);
  return Number(o.price) || 0;
}

// ── 智聊 ChatX（AI 客服成交引擎；sku_registry zhiliao/chatx-*）────────────────
export const CHATX_TIERS: LineTier[] = [
  {
    key: "autochat-entry",
    edition: "standard",
    monthly: priceOf(autochatOffers, "autochat-entry"),
    name: { zh: "入门版 Entry", en: "Entry" },
    audience: { zh: "个人 / 小团队起步", en: "Solo / small team" },
    feats: {
      zh: ["3 个聊天账号", "AI 自动翻译", "1 个平台（默认 Telegram）", "AI 拟稿 · 人审后发"],
      en: ["3 chat accounts", "AI translation", "1 platform (Telegram default)", "AI drafts · human review"],
    },
  },
  {
    key: "autochat-team",
    edition: "pro",
    monthly: priceOf(autochatOffers, "autochat-team"),
    hot: true,
    name: { zh: "团队版 Team", en: "Team" },
    audience: { zh: "跨境销售团队", en: "Cross-border sales teams" },
    feats: {
      zh: ["10 个聊天账号", "全平台（TG / WA / LINE / Messenger / Web）", "AI 自动成交 · 全自动回复", "主动跟进 · 语音消息"],
      en: ["10 chat accounts", "All platforms (TG / WA / LINE / Messenger / Web)", "AI auto-closing · full auto-reply", "Proactive follow-up · voice messages"],
    },
  },
  {
    key: "autochat-flagship",
    edition: "enterprise",
    monthly: priceOf(autochatOffers, "autochat-flagship"),
    name: { zh: "旗舰版 Flagship", en: "Flagship" },
    audience: { zh: "企业 / 多团队", en: "Enterprise / multi-team" },
    feats: {
      zh: ["50 个聊天账号", "团队版全部能力", "人工接管工作台", "数据看板 · 漏斗分析"],
      en: ["50 chat accounts", "Everything in Team", "Human takeover workspace", "Dashboards · funnel analytics"],
    },
  },
];

// ── 通译 LingoX（多平台聊天翻译；sku_registry tongyi/lingox-*）────────────────
export const LINGOX_TIERS: LineTier[] = [
  {
    key: "translate-charpack",
    edition: "standard",
    monthly: priceOf(translateOffers, "translate-charpack"),
    oneTime: true,
    name: { zh: "字符包 Char pack", en: "Char pack" },
    audience: { zh: "一次性 · 按量加购", en: "One-time top-up" },
    feats: {
      zh: ["150 万翻译字符", "术语锁定 · 翻译记忆", "会员中心粘贴凭证即到账", "需已有通译订阅"],
      en: ["1.5M translation chars", "Term-lock glossary · translation memory", "Redeem in membership center", "Requires an active LingoX plan"],
    },
  },
  {
    key: "translate-team",
    edition: "standard",
    monthly: priceOf(translateOffers, "translate-team"),
    hot: true,
    name: { zh: "团队版 Team", en: "Team" },
    audience: { zh: "多坐席客服 / 销售", en: "Multi-seat support / sales" },
    feats: {
      // 300 万字符/月与引擎签发额度同源（chatx_fulfillment.LINGOX_SKU_SPECS）；改两处一起改。
      zh: ["300 万字符 / 月", "5 坐席 · 统一收件箱", "客户 journey · 漏斗计数", "全平台双向翻译"],
      en: ["3M chars / mo", "5 seats · unified inbox", "Customer journey · funnel counter", "Two-way translation, all platforms"],
    },
  },
  {
    key: "translate-pro",
    edition: "pro",
    monthly: priceOf(translateOffers, "translate-pro"),
    name: { zh: "专业版 Pro", en: "Pro" },
    audience: { zh: "重度翻译工作流", en: "Heavy translation workloads" },
    feats: {
      zh: ["不限字符", "15 坐席", "多模态（图片 / 语音）翻译", "置信度徽标 · 引擎健康看板"],
      en: ["Unlimited chars", "15 seats", "Multimodal (image / voice) translate", "Confidence badge · engine health"],
    },
  },
];

/** 产品线元信息：Tab 文案 + 面板首段（AvatarHub 的「不按字符计费」话术只对本机算力
 *  产品成立，LingoX 按字符额度计量——文案必须随产品线切换，防承诺错位）。 */
export interface FamilyMeta {
  key: OrderFamily;
  tab: { zh: string; en: string };
  blurb: { zh: string; en: string };
}

export const FAMILIES: FamilyMeta[] = [
  {
    key: "avatarhub",
    tab: { zh: "幻境数字人", en: "AvatarHub" },
    blurb: {
      zh: "引擎跑在你自己的设备上——我们不卖算力，所以不按字符、张数、时长计费，用量不限。设备自备（下方有配置与配件清单），我们协助部署；到账后按机器指纹签发授权，客户端一键激活。",
      en: "The engine runs on your own hardware — we don't sell compute, so there's no per-character or per-minute metering. Bring your device (specs below), we help you deploy; licenses are issued against your machine fingerprint after payment.",
    },
  },
  {
    key: "chatx",
    tab: { zh: "智聊 ChatX", en: "ChatX" },
    blurb: {
      zh: "AI 客服成交引擎：多平台账号统一接管，AI 自动回复、主动跟进、引导成交，人工可随时接管。按账号数与平台授权，到账自动开通，粘贴授权码即激活。",
      en: "AI closing engine: unify accounts across platforms with AI auto-reply, proactive follow-up and guided closing — humans can take over anytime. Licensed by accounts and platforms; auto-activated after payment.",
    },
  },
  {
    key: "lingox",
    tab: { zh: "通译 LingoX", en: "LingoX" },
    blurb: {
      zh: "多平台聊天双向翻译：按坐席 + 字符额度授权（专业版不限字符），术语锁定与翻译记忆保持口径一致。字符包一次性加量，会员中心粘贴凭证即到账。",
      en: "Two-way chat translation across platforms, licensed by seats + character quota (Pro is unlimited). Term-lock glossary and translation memory keep wording consistent; top-up packs credit instantly in the membership center.",
    },
  },
];

const TIERS_BY_FAMILY: Record<Exclude<OrderFamily, "avatarhub">, LineTier[]> = {
  chatx: CHATX_TIERS,
  lingox: LINGOX_TIERS,
};

export function familyTiers(family: OrderFamily, avatarhubTiers: Tier[]): LineTier[] {
  return family === "avatarhub" ? avatarhubTiers : TIERS_BY_FAMILY[family];
}

/** 深链 plan 参数 → 所属产品线（找不到 = undefined，面板保持默认）。 */
export function familyOfPlan(plan: string, avatarhubTiers: Tier[]): OrderFamily | undefined {
  const p = String(plan || "").trim().toLowerCase();
  if (!p) return undefined;
  if (avatarhubTiers.some((t) => t.key === p)) return "avatarhub";
  if (CHATX_TIERS.some((t) => t.key === p)) return "chatx";
  if (LINGOX_TIERS.some((t) => t.key === p)) return "lingox";
  return undefined;
}

/** 每条产品线的默认选中档（hot 优先，无 hot 取首个）。 */
export function familyDefaultTier(family: OrderFamily, avatarhubTiers: Tier[]): string {
  const tiers = familyTiers(family, avatarhubTiers);
  return (tiers.find((t) => t.hot) ?? tiers[0]).key;
}
