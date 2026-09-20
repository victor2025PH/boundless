// /console 服务端专用：SKU / 档位 → 中文名（授权台账、订单台账、客户档案共用）。
//
// 数据源 = lib/generated/product-facts.json 内嵌的 sku_registry（由 scripts/sync-product-facts.mjs
// 从 platform/licensing/sku_registry.json 同步，随站点一起部署；名称已自带「（停售）」标记）。
// 只在服务端组件 import（JSON ~1.7k 行，不进浏览器包；仓内未装 server-only 包，靠约定：
// 任何 "use client" 文件不得 import 本模块）；纯常量 labels.ts 才是客户端可用的那份。
import productFacts from "@/lib/generated/product-facts.json";
import { LICENSE_EDITION_LABEL, LICENSE_PLAN_LABEL, PRODUCT_LABEL, lbl } from "./labels";

interface FlatSku {
  product: string;
  sku_id: string;
  name: string;
  unit?: string;
  price?: string;
  note?: string;
}

const FLAT: FlatSku[] = (() => {
  try {
    const reg = (productFacts as { sku_registry?: { flat_skus?: FlatSku[] } }).sku_registry;
    return Array.isArray(reg?.flat_skus) ? reg!.flat_skus! : [];
  } catch {
    return [];
  }
})();
const BY_ID = new Map(FLAT.map((s) => [s.sku_id, s]));

/** sku_id → 注册表中文名（含「（停售）」）；未登记返回 null。 */
export function skuName(skuId: string | null | undefined): string | null {
  if (!skuId) return null;
  return BY_ID.get(skuId)?.name ?? null;
}

/** sku_id → 所属产品 id（zhiliao / tongyi / huansheng …）；未登记返回 null。 */
export function skuProduct(skuId: string | null | undefined): string | null {
  if (!skuId) return null;
  return BY_ID.get(skuId)?.product ?? null;
}

/** 是否停售 SKU（注册表名称带「停售」）。 */
export function skuDiscontinued(skuId: string | null | undefined): boolean {
  const n = skuName(skuId);
  return !!n && n.includes("停售");
}

/**
 * 授权 / 订单行的「档位」显示：
 *   1. sku_id 在注册表 → 注册表名（如「基础版（停售）」「充值 200U」）；
 *   2. 否则 chengjie 用 plan 中文（基础版 / 专业版 / 旗舰版 / 社区版），avatarhub 用 edition 中文；
 *   3. 都没有 → "—"。
 * 原始英文值统一进 title，供排障核对。
 */
export function tierLabel(row: {
  sku_id?: string | null;
  plan?: string | null;
  edition?: string | null;
  period?: string | null;
}): { text: string; title?: string } {
  const rawBits = [row.sku_id, row.plan, row.edition, row.period].filter(Boolean).join(" / ");
  const bySku = skuName(row.sku_id);
  if (bySku) return { text: bySku, title: rawBits || undefined };
  const plan = row.plan ? lbl(LICENSE_PLAN_LABEL, row.plan) : "";
  const edition = row.edition ? lbl(LICENSE_EDITION_LABEL, row.edition) : "";
  const text = [plan, edition].filter(Boolean).join(" · ");
  return { text: text || "—", title: rawBits || undefined };
}

/**
 * 产品显示名：product_id 优先；缺 product_id 时按 sku_id 反查；再缺则回退到承载引擎名。
 */
export function productDisplay(row: { product_id?: string | null; sku_id?: string | null }, engineFallback: string): {
  text: string;
  title?: string;
} {
  const pid = row.product_id || skuProduct(row.sku_id);
  if (pid) return { text: lbl(PRODUCT_LABEL, pid), title: pid };
  return { text: engineFallback };
}
