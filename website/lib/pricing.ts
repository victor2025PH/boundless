// Canonical price figures, single source of truth.
//
// These are the headline SKU numbers surfaced in structured data (schema.org
// JSON-LD in app/layout.tsx) and in headline marketing copy. The localized
// display strings in lib/content.ts (e.g. "980 起", "198 / 月") are presentation
// formats of these same numbers — when a price changes, update it HERE and keep
// the content.ts display strings in sync.

import { LINGOX_WORKBENCH, chatxSchemaOffers } from "./chatx-pricing";

export type PriceUnit = "one-time" | "month";

export interface PriceOffer {
  id: string;
  /** 对应 platform/licensing/sku_registry.json（全域 SKU 唯一注册表，由 products/<产品>/product.yaml
   *  汇总生成，消费入口 platform/licensing/sku_registry.py）中的 sku_id ——
   *  官网报价与集团统一授权台账的关联键。2026-07-18 定价决议（按竞品 ×2）生效后，
   *  7 个 offer 已全部回填 skuId 且两侧价格/币种一致（同日治理收尾补挂
   *  voiceOffers / livexOffers / chatx-entry 后共 13 个，全部带 skuId），
   *  见 platform/licensing/SKU_ALIGNMENT_REPORT.md"定价决议落地"与"治理收尾"小节。
   *  本字段不进 schema.org 输出（toSchemaOffer 未映射）。 */
  skuId?: string;
  name: string;
  /** Numeric string (no currency / suffix) so it is valid for schema.org Offer.price. */
  price: string;
  /** ISO 4217 quote currency. 2026-07-18 定价决议：全部 offer 统一以 USD 报价
   *  （schema.org priceCurrency 要求 ISO 4217）；USDT 仅作为"结算方式/支付轨道"
   *  在文案层表述，不再作为报价币种出现。 */
  currency: "USD";
  unit: PriceUnit;
  description: string;
}

/** Real-time face & voice swap — private deployment.
 *  ⚠ 2026-08-04 定价改版：官网对外不再展示私有部署固定价（realtime 版块与 engage 卡
 *  已改「咨询报价」，与幻境 STUDIO 旗舰版口径一致）；本数组保留仅供台账/offer-map 反查
 *  历史订单的 skuId 映射，不再进任何页面展示或 JSON-LD。改回挂牌需产品决策。
 *  报价币种已统一 USD（USDT 仅保留为结算方式表述）。skuId 对齐说明：
 *  realtime-basic ↔ registry `facex-live-deploy`；realtime-creator ↔ registry
 *  `livex-creator-deploy`。详见 platform/licensing/SKU_ALIGNMENT_REPORT.md。 */
export const realtimeOffers: PriceOffer[] = [
  {
    id: "realtime-basic",
    skuId: "facex-live-deploy",
    name: "Basic deployment",
    price: "980",
    currency: "USD",
    unit: "one-time",
    description:
      "One-time; real-time face swap OR voice clone, remote deploy + tuning + training + support.",
  },
  {
    // 2026-07-18 定价决议：registry 已补 livex-creator-deploy（products/huanying/product.yaml），
    // 本 offer 回填 skuId 并改价 2580→5580（竞品栈 ×2，见上方数组注释）。
    id: "realtime-creator",
    skuId: "livex-creator-deploy",
    name: "Creator all-in deployment",
    price: "5580",
    currency: "USD",
    unit: "one-time",
    description:
      "One-time; face swap + voice + digital human, multi-scenario deep tuning, 30-day support.",
  },
];

/** Voice cloning & TTS (幻声 VoiceX) — ⚠ 2026-08-04 定价改版后**仅供台账反查**：
 *  声音能力已并入幻境 STUDIO 五档会员（lib/avatarhub-pricing.ts::TIERS 为展示/JSON-LD
 *  单一真相），本数组的 18/78/198 旧价不再进任何页面或结构化数据，保留只为
 *  findOfferBySkuId 反查历史订单的 skuId 映射。改回挂牌需产品决策。
 *  （历史口径：2026-07-18 治理收尾，价格取自 registry voicex-*，USD/month。） */
export const voiceOffers: PriceOffer[] = [
  {
    id: "voice-starter",
    skuId: "voicex-starter",
    name: "Starter",
    price: "18",
    currency: "USD",
    unit: "month",
    description: "Per month; 1 cloned voice, 10k TTS chars.",
  },
  {
    id: "voice-std",
    skuId: "voicex-std",
    name: "Standard",
    price: "78",
    currency: "USD",
    unit: "month",
    description: "Per month; 5 cloned voices, 100k TTS chars, multilingual.",
  },
  {
    id: "voice-pro",
    skuId: "voicex-pro",
    name: "Pro",
    price: "198",
    currency: "USD",
    unit: "month",
    description: "Per month; 20 cloned voices, 500k TTS chars, voice-changer API.",
  },
];

/** Digital human (幻影 LiveX) — ⚠ 2026-08-04 定价改版后**仅供台账反查**：
 *  数字人能力已并入幻境 STUDIO 五档会员（展示/JSON-LD 由 avatarhub-pricing.TIERS 派生），
 *  798 买断 / 398 月付旧价不再进任何页面展示，保留只为台账/offer-map 反查历史订单。
 *  （历史口径：2026-07-18 治理收尾，价格取自 registry livex-*。） */
export const livexOffers: PriceOffer[] = [
  {
    id: "livex-avatar-buy",
    skuId: "livex-avatar-buy",
    name: "Avatar buyout",
    price: "798",
    currency: "USD",
    unit: "one-time",
    description: "One-time; permanent digital-human avatar with bound cloned voice.",
  },
  {
    id: "livex-dub-matrix",
    skuId: "livex-dub-matrix",
    name: "Short-video matrix",
    price: "398",
    currency: "USD",
    unit: "month",
    description: "Per month; 30 dubbed short videos (100-video tier available).",
  },
];

/** AI auto-closing chat system — Token 分层体系（2026-08-19 定价决议）。
 *  数字单一真相在 lib/chatx-pricing.ts::CHATX_PLANS / TOKEN_PACKS，本数组按
 *  schema.org PriceOffer 形状派生（个人 39 / 团队 49 每坐席 / 旗舰 598），
 *  layout.tsx JSON-LD 与 order-lines 消费。改价改 chatx-pricing.ts，勿在此手写数字。
 *  旧 chatx-entry(58)/chatx-team(198) 停售 → legacyAutochatOffers 台账反查。 */
export const autochatOffers: PriceOffer[] = chatxSchemaOffers()
  .filter((o) => o.unit === "month")
  .map((o) => ({
    id: o.id,
    skuId: o.skuId,
    name: o.name,
    price: o.price,
    currency: "USD",
    unit: "month",
    description: o.description,
  }));

/** Token 包（一次性 · 12 个月有效 · 跨 ChatX/LingoX 通用）——同样派生自 chatx-pricing.ts。 */
export const tokenPackOffers: PriceOffer[] = chatxSchemaOffers()
  .filter((o) => o.unit === "one-time")
  .map((o) => ({
    id: o.id,
    skuId: o.skuId,
    name: o.name,
    price: o.price,
    currency: "USD",
    unit: "one-time",
    description: o.description,
  }));

/** 通译 LingoX（2026-08-19 翻译免费化决议）：标准翻译永久免费不限量（不产生 offer，
 *  公平使用 200 万字符/日/授权）；专业翻译按 Token 计量（10/千字符，DeepL 认证 40/千字符，
 *  无订阅 SKU）；唯一订阅挂牌 = 翻译工作台（每坐席/月，纯翻译团队）。
 *  旧 charpack(59)/team(99)/pro(198) 停售 → legacyTranslateOffers 台账反查；
 *  charpack 未用完字符按 1.5M = 60,000 Token 等值换发。 */
export const translateOffers: PriceOffer[] = [
  {
    id: LINGOX_WORKBENCH.key,
    skuId: LINGOX_WORKBENCH.skuId,
    name: "Translation Workbench",
    price: String(LINGOX_WORKBENCH.monthly),
    currency: "USD",
    unit: "month",
    description:
      "Per month per seat; translation-only teams: multi-seat unified inbox, customer journey, funnel counter. Standard translation is free & unlimited in every plan.",
  },
];

/** ⚠ 2026-08-19 停售台账（勿删）：仅供 findOfferBySkuId 反查历史订单，不进任何页面/JSON-LD。 */
export const legacyAutochatOffers: PriceOffer[] = [
  { id: "autochat-entry", skuId: "chatx-entry", name: "Entry (legacy)", price: "58", currency: "USD", unit: "month", description: "Discontinued 2026-08-19; superseded by ChatX Personal (39/mo)." },
  { id: "autochat-team", skuId: "chatx-team", name: "Team (legacy)", price: "198", currency: "USD", unit: "month", description: "Discontinued 2026-08-19; superseded by ChatX Team per-seat (49/seat/mo)." },
];

export const legacyTranslateOffers: PriceOffer[] = [
  { id: "translate-charpack", skuId: "lingox-charpack", name: "Char pack (legacy)", price: "59", currency: "USD", unit: "one-time", description: "Discontinued 2026-08-19; unused chars convert to 60,000 tokens." },
  { id: "translate-team", skuId: "lingox-team", name: "Team (legacy)", price: "99", currency: "USD", unit: "month", description: "Discontinued 2026-08-19; superseded by Workbench (29/seat/mo) + free standard translation." },
  { id: "translate-pro", skuId: "lingox-pro", name: "Pro (legacy)", price: "198", currency: "USD", unit: "month", description: "Discontinued 2026-08-19; unlimited chars superseded by free standard translation." },
];

/** Map a PriceOffer to a schema.org Offer node. */
export function toSchemaOffer(o: PriceOffer) {
  return {
    "@type": "Offer",
    name: o.name,
    price: o.price,
    priceCurrency: o.currency,
    description: o.description,
  };
}

// 全部对外挂牌 + 停售台账 offer 数组（新增数组时同步登记，findOfferBySkuId 才能反查到；
// legacy 数组排在最后——同 skuId 时挂牌价优先命中）。
const ALL_OFFER_ARRAYS: readonly (readonly PriceOffer[])[] = [
  realtimeOffers,
  voiceOffers,
  livexOffers,
  autochatOffers,
  tokenPackOffers,
  translateOffers,
  legacyAutochatOffers,
  legacyTranslateOffers,
];

/** 按全域 SKU id（platform/licensing/sku_registry.json 的 sku_id）反查官网 offer。
 *  纯只读遍历，供集团授权台账 / 后台对账反查用；未对齐（skuId 留空）的 offer
 *  查不到，返回 undefined。 */
export function findOfferBySkuId(skuId: string): PriceOffer | undefined {
  for (const offers of ALL_OFFER_ARRAYS) {
    const hit = offers.find((o) => o.skuId === skuId);
    if (hit) return hit;
  }
  return undefined;
}
