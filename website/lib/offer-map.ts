// 订单 plan/edition/period → 全域 SKU / 产品的静态映射（宁缺毋错）。
//
// skuId / productId 对应 platform/licensing/sku_registry.json（全域 SKU 唯一注册表）。
// 映射依据 lib/pricing.ts 各 offer 的 skuId 对齐关系（pricing.ts 是报价单一真相源；
// 本表是订单侧的静态快照，刻意不 import pricing.ts —— 价格/币种随时在改，
// offer id ↔ sku_id 的对应关系才是稳定事实）。
// AvatarHub 会员档（plan=trial/starter/standard/pro/flagship × edition=trial/standard/
// pro/enterprise）属整机引擎、跨幻声/幻颜/幻影多产品，映射不到单一 SKU → 一律 null。
//
// ⚠️ ORDER_SKU_MAP 条目与 scripts/ledger-lib.mjs 中的同名映射表文本一致（纯 JS
// 回填脚本不经 TS 编译、无法 import 本文件），修改必须两处同步！

export interface OrderSkuRef {
  /** 全域 SKU 关联键（sku_registry.json 的 sku_id），映射不到为 null。 */
  skuId: string | null;
  /** 全域产品 id（zhituo/zhiliao/tongyi/tongchuan/huansheng/huanying/huanyan/website），映射不到为 null。 */
  productId: string | null;
}

// ⚠️ 与 scripts/ledger-lib.mjs 的 ORDER_SKU_MAP 逐条同步！
const ORDER_SKU_MAP: Record<string, { skuId: string; productId: string }> = {
  // 通译 LingoX（pricing.ts translateOffers → registry tongyi/lingox-*）
  "translate-charpack": { skuId: "lingox-charpack", productId: "tongyi" },
  "translate-team": { skuId: "lingox-team", productId: "tongyi" },
  "translate-pro": { skuId: "lingox-pro", productId: "tongyi" },
  // 智聊 ChatX（pricing.ts autochatOffers → registry zhiliao/chatx-*）
  "autochat-team": { skuId: "chatx-team", productId: "zhiliao" },
  "autochat-flagship": { skuId: "chatx-flagship", productId: "zhiliao" },
  // 实时部署（pricing.ts realtimeOffers）：basic → 幻颜实时换脸部署；
  // creator → 幻影创作者全能部署（registry 2026-07 新增 livex-creator-deploy）
  "realtime-basic": { skuId: "facex-live-deploy", productId: "huanyan" },
  "realtime-creator": { skuId: "livex-creator-deploy", productId: "huanying" },
};

/** 下单参数 → 全域 SKU（宽松容错：大小写 / 首尾空白；未命中一律 null，宁缺毋错）。
 *  edition/period 现阶段不参与判定（预留：同 plan 将来按周期拆 SKU 时启用）。
 *  ⚠️ 与 scripts/ledger-lib.mjs 的 resolveOrderSku 逻辑一致，修改必须同步！ */
export function resolveOrderSku(plan: string, edition?: string, period?: string): OrderSkuRef {
  void edition;
  void period;
  const key = String(plan ?? "").trim().toLowerCase();
  const hit = key && Object.prototype.hasOwnProperty.call(ORDER_SKU_MAP, key) ? ORDER_SKU_MAP[key] : undefined;
  return hit ? { skuId: hit.skuId, productId: hit.productId } : { skuId: null, productId: null };
}
