// Canonical price figures, single source of truth.
//
// These are the headline SKU numbers surfaced in structured data (schema.org
// JSON-LD in app/layout.tsx) and in headline marketing copy. The localized
// display strings in lib/content.ts (e.g. "980 起", "198 / 月") are presentation
// formats of these same numbers — when a price changes, update it HERE and keep
// the content.ts display strings in sync.

export type PriceUnit = "one-time" | "month";

export interface PriceOffer {
  id: string;
  /** 对应 platform/licensing/sku_registry.json（全域 SKU 唯一注册表，由 products/<产品>/product.yaml
   *  汇总生成，消费入口 platform/licensing/sku_registry.py）中的 sku_id ——
   *  官网报价与集团统一授权台账的关联键。2026-07-18 定价决议（按竞品 ×2）生效后，
   *  7 个 offer 已全部回填 skuId 且两侧价格/币种一致，
   *  见 platform/licensing/SKU_ALIGNMENT_REPORT.md"定价决议落地"小节。
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
 *  报价币种已统一 USD（USDT 仅保留为结算方式表述）。skuId 对齐说明：
 *  realtime-basic ↔ registry `facex-live-deploy`（同为"实时换脸部署 980 / one-time"，
 *  registry 计 "from 980" 起价，差异只记录不改价）；realtime-creator ↔ registry
 *  `livex-creator-deploy`（2026-07-18 定价决议新增，5580 USD：基准栈 Synthesia Studio
 *  Avatar $1000/年 + ElevenLabs Pro ≈$1188/年 + HeyGen LiveAvatar ≈$588/年 ≈ $2776/年
 *  ×2=5552 → 尾数 8 惯例 5580）。详见 platform/licensing/SKU_ALIGNMENT_REPORT.md。 */
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

/** AI auto-closing chat system — subscription.
 *  skuId 对齐 registry 智聊 ChatX：价格数值/周期一致；2026-07-18 定价决议起报价币种
 *  统一 USD（与 product.yaml"单位 USD、结算支持 USDT"口径一致），币种差异已消除。
 *  registry 的 chatx-entry（入门 58/月）未在本文件挂牌，见 SKU_ALIGNMENT_REPORT.md。 */
export const autochatOffers: PriceOffer[] = [
  {
    id: "autochat-team",
    skuId: "chatx-team",
    name: "Team",
    price: "198",
    currency: "USD",
    unit: "month",
    description: "Per month; 10 chat accounts, all platforms, AI auto-closing replies.",
  },
  {
    id: "autochat-flagship",
    skuId: "chatx-flagship",
    name: "Flagship",
    price: "598",
    currency: "USD",
    unit: "month",
    description: "Per month; 50 accounts, human handoff, dashboard, persona voice.",
  },
];

/** Real-time cross-border translation SCRM (通译 LingoX) — flagship, low-risk cash flow.
 *  USD, self-serve. Differentiator vs. plain translation add-ons: term-lock glossary,
 *  translation memory, and customer-asset SCRM (unified inbox + journey + funnel).
 *  skuId 对齐 registry 通译 LingoX：三档 id/币种/周期一一对应。2026-07-18 定价决议
 *  （竞品 NexScrm ×2 + 品牌尾数惯例）：charpack $30×2=60→59；team $48×2=96→99；
 *  pro $90×2=180→198。registry（products/tongyi/product.yaml）已同步实价，TBD 差异已消除。 */
export const translateOffers: PriceOffer[] = [
  {
    id: "translate-charpack",
    skuId: "lingox-charpack",
    name: "Char pack",
    price: "59",
    currency: "USD",
    unit: "one-time",
    description:
      "One-time; 1.5M translation chars, term-lock glossary + translation memory.",
  },
  {
    id: "translate-team",
    skuId: "lingox-team",
    name: "Team",
    price: "99",
    currency: "USD",
    unit: "month",
    description:
      "Per month; multi-seat unified inbox, customer journey, conversion funnel counter.",
  },
  {
    id: "translate-pro",
    skuId: "lingox-pro",
    name: "Pro",
    price: "198",
    currency: "USD",
    unit: "month",
    description:
      "Per month; unlimited chars, multimodal (image/voice) translate, confidence badge + engine health.",
  },
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

// 全部对外挂牌的 offer 数组（新增数组时同步登记，findOfferBySkuId 才能反查到）。
const ALL_OFFER_ARRAYS: readonly (readonly PriceOffer[])[] = [
  realtimeOffers,
  autochatOffers,
  translateOffers,
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
