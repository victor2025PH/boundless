// 跨售商机引擎（Cross-sell Opportunities）—— 人设总线的第一个变现动作（P5 落点）。
//
// 核心洞察：客户在某产品创建了人设（如幻声克隆了声音），但未授权其他能用该人设
// 的产品（幻影开播/通传开会）——这是最便宜的跨售信号。本文件是**纯只读**规则
// 引擎：只 SELECT group-ledger.db（复用 getLedgerDb 连接），不写任何表；空库或
// 查询失败一律返回空数组（fail-soft），绝不影响 console 渲染与 API 主链路。
//
// 三类规则（可扩展：每类一个纯函数，统一产出 Opportunity 行，collect 汇总）：
//   1. persona_cross_sell     人设槽位能支撑、但该 persona 未授权且客户未购的产品（信号最强）；
//   2. product_gap_cross_sell 买了 A 未买同系互补品 B（智连系/通达系/幻境系）；
//   3. expiring_renewal       30 天内到期的授权 → 续费（复用 ledger listLicenses 到期口径）。
//
// 隐私约束：evidence 只放 id / 计数 / 产品名 / 槽位名，不放联系方式与聊天内容。
// 「标记已跟进」需要 opportunities_log 落库表（下阶段），本轮只读展示。

import type Database from "better-sqlite3";
import { getLedgerDb, listLicenses } from "./ledger";
import {
  PERSONA_PRODUCT_IDS,
  PERSONA_SLOTS,
  type PersonaProductId,
  type PersonaSlot,
} from "./personas";

// ── 商机类型 ────────────────────────────────────────────────────────
export const OPPORTUNITY_KINDS = [
  "persona_cross_sell",
  "product_gap_cross_sell",
  "expiring_renewal",
] as const;
export type OpportunityKind = (typeof OPPORTUNITY_KINDS)[number];

export function isOpportunityKind(v: string): v is OpportunityKind {
  return (OPPORTUNITY_KINDS as readonly string[]).includes(v);
}

export interface Opportunity {
  kind: OpportunityKind;
  customerId: string;
  customerName: string | null;
  /** persona_cross_sell：贡献信号的人设（多个时取第一个，全量在 evidence.personaIds）。 */
  personaId?: string;
  /** 跨售起点：已授权/已购产品 id；人设无任何授权时回退来源引擎；续费类=到期产品本身。 */
  fromProduct: string;
  toProduct: string;
  reason: string;
  /** 粗排序权重：persona_cross_sell 90 > expiring_renewal 70+紧迫度 > gap 50。 */
  signalValue: number;
  /** 只放 id / 计数 / 产品名 / 槽位名 —— 不放联系方式、聊天内容。 */
  evidence: Record<string, string | number | string[]>;
}

// ── 规则映射（可扩展的静态知识）────────────────────────────────────
/** 槽位 → 可跨售产品：该槽位资产点亮后，哪些产品能直接复用它。 */
export const SLOT_CROSS_SELL_MAP: Record<PersonaSlot, PersonaProductId[]> = {
  voice: ["huansheng", "huanying", "tongchuan"], // 声纹 → 配音 / 直播口型 / 同传配音
  face: ["huanying", "huanyan"], // 脸模 → 直播分身 / 换脸
  prompt: ["zhiliao", "tongyi", "zhituo"], // 话术人格 → AI 承接 / 术语 / 获客话术
  knowledge: ["tongyi", "zhiliao"], // 知识库 → 术语 / 客服知识
};

/** 同系互补品（族内买 A 未买 B 即缺口；三族互不相交）。 */
export const PRODUCT_FAMILIES: { name: string; products: PersonaProductId[] }[] = [
  { name: "智连系", products: ["zhituo", "zhiliao"] },
  { name: "通达系", products: ["tongyi", "tongchuan"] },
  { name: "幻境系", products: ["huansheng", "huanying", "huanyan"] },
];

const PRODUCT_NAMES: Record<string, string> = {
  zhituo: "智拓",
  zhiliao: "智聊",
  tongyi: "通译",
  tongchuan: "通传",
  huansheng: "幻声",
  huanying: "幻影",
  huanyan: "幻颜",
  website: "官网服务",
};

/** 产品中文名（未知 id 原样返回）。 */
export function productName(id: string): string {
  return PRODUCT_NAMES[id] ?? id;
}

/** 展示用「中文名 + id」（未知 id 原样返回，如引擎名 avatarhub）。 */
export function productLabel(id: string): string {
  const cn = PRODUCT_NAMES[id];
  return cn ? `${cn} ${id}` : id;
}

const SLOT_LABELS: Record<PersonaSlot, string> = {
  face: "face 形象",
  voice: "voice 声纹",
  prompt: "prompt 话术",
  knowledge: "knowledge 知识库",
};

const SLOT_COLUMNS: Record<PersonaSlot, "slot_face" | "slot_voice" | "slot_prompt" | "slot_knowledge"> = {
  face: "slot_face",
  voice: "slot_voice",
  prompt: "slot_prompt",
  knowledge: "slot_knowledge",
};

// ── 共享只读查询 ────────────────────────────────────────────────────
/** 客户已购产品集合：orders 成交（paid/activated）∪ licenses（任意状态——买过就算）。 */
function ownedProductsByCustomer(db: Database.Database): Map<string, Set<string>> {
  const map = new Map<string, Set<string>>();
  const add = (cid: string, pid: string) => {
    let set = map.get(cid);
    if (!set) map.set(cid, (set = new Set()));
    set.add(pid);
  };
  type Row = { customer_id: string; product_id: string };
  for (const r of db
    .prepare(
      "SELECT customer_id, product_id FROM orders WHERE customer_id IS NOT NULL AND product_id IS NOT NULL AND status IN ('paid','activated')"
    )
    .all() as Row[]) {
    add(r.customer_id, r.product_id);
  }
  for (const r of db
    .prepare(
      "SELECT customer_id, product_id FROM licenses WHERE customer_id IS NOT NULL AND product_id IS NOT NULL"
    )
    .all() as Row[]) {
    add(r.customer_id, r.product_id);
  }
  return map;
}

function customerNameMap(db: Database.Database): Map<string, string | null> {
  const m = new Map<string, string | null>();
  for (const r of db.prepare("SELECT id, display_name FROM customers").all() as {
    id: string;
    display_name: string | null;
  }[]) {
    m.set(r.id, r.display_name);
  }
  return m;
}

// ── 规则 1：persona_cross_sell ──────────────────────────────────────
interface PersonaJoinRow {
  id: string;
  customer_id: string;
  source_system: string;
  slot_face: number;
  slot_voice: number;
  slot_prompt: number;
  slot_knowledge: number;
  customer_name: string | null;
}

/** 客户名下 active 人设：槽位支撑的产品集合 − 该 persona 已 grant − 客户已购
 *  = 建议跨售。按 (customer, toProduct) 去重（多个人设支撑同一建议时合并计数）。
 *  purge_pending/purged/archived 的人设不产生信号（资产在清除或已封存）。 */
export function personaCrossSellOpportunities(db: Database.Database = getLedgerDb()): Opportunity[] {
  const personas = db
    .prepare(
      `SELECT p.id, p.customer_id, p.source_system,
              p.slot_face, p.slot_voice, p.slot_prompt, p.slot_knowledge,
              c.display_name AS customer_name
       FROM personas p JOIN customers c ON c.id = p.customer_id
       WHERE p.customer_id IS NOT NULL AND p.status = 'active'`
    )
    .all() as PersonaJoinRow[];
  if (!personas.length) return [];

  const grants = new Map<string, Set<string>>();
  for (const g of db
    .prepare("SELECT persona_id, product_id FROM persona_grants WHERE revoked_at IS NULL")
    .all() as { persona_id: string; product_id: string }[]) {
    let set = grants.get(g.persona_id);
    if (!set) grants.set(g.persona_id, (set = new Set()));
    set.add(g.product_id);
  }
  const owned = ownedProductsByCustomer(db);

  interface Agg {
    customerId: string;
    customerName: string | null;
    toProduct: PersonaProductId;
    personaIds: string[];
    slots: Set<PersonaSlot>;
    fromProduct: string;
    grantedProducts: Set<string>;
  }
  const agg = new Map<string, Agg>();

  for (const p of personas) {
    const litSlots = PERSONA_SLOTS.filter((slot) => p[SLOT_COLUMNS[slot]] > 0);
    if (!litSlots.length) continue;
    const granted = grants.get(p.id) ?? new Set<string>();
    const bought = owned.get(p.customer_id) ?? new Set<string>();
    // 该 persona 的跨售起点：已授权产品中最靠前的一个；一个都没授权时用来源引擎。
    const from = PERSONA_PRODUCT_IDS.find((pid) => granted.has(pid)) ?? p.source_system;

    const supporting = new Map<PersonaProductId, PersonaSlot[]>();
    for (const slot of litSlots) {
      for (const to of SLOT_CROSS_SELL_MAP[slot]) {
        if (granted.has(to) || bought.has(to)) continue;
        const arr = supporting.get(to);
        if (arr) arr.push(slot);
        else supporting.set(to, [slot]);
      }
    }
    for (const [to, slots] of supporting) {
      const key = `${p.customer_id}|${to}`;
      let a = agg.get(key);
      if (!a) {
        agg.set(
          key,
          (a = {
            customerId: p.customer_id,
            customerName: p.customer_name,
            toProduct: to,
            personaIds: [],
            slots: new Set(),
            fromProduct: from,
            grantedProducts: new Set(),
          })
        );
      }
      a.personaIds.push(p.id);
      for (const s of slots) a.slots.add(s);
      for (const g of granted) a.grantedProducts.add(g);
    }
  }

  const out: Opportunity[] = [];
  for (const a of agg.values()) {
    const slotList = PERSONA_SLOTS.filter((s) => a.slots.has(s));
    const n = a.personaIds.length;
    out.push({
      kind: "persona_cross_sell",
      customerId: a.customerId,
      customerName: a.customerName,
      personaId: a.personaIds[0],
      fromProduct: a.fromProduct,
      toProduct: a.toProduct,
      reason: `已有 ${slotList.map((s) => SLOT_LABELS[s]).join("、")} 槽位人设${
        n > 1 ? `（${n} 个）` : ""
      }，建议开通「${productName(a.toProduct)}」`,
      signalValue: 90,
      evidence: {
        personaIds: a.personaIds,
        personaCount: n,
        slots: slotList,
        grantedProducts: [...a.grantedProducts].sort(),
        suggestedProduct: a.toProduct,
      },
    });
  }
  return out;
}

// ── 规则 2：product_gap_cross_sell ──────────────────────────────────
/** 买了族内 A（成交订单或任意授权）却缺同族 B → 每个缺口一行（族互不相交，天然去重）。 */
export function productGapOpportunities(db: Database.Database = getLedgerDb()): Opportunity[] {
  const owned = ownedProductsByCustomer(db);
  if (!owned.size) return [];
  const names = customerNameMap(db);
  const out: Opportunity[] = [];
  for (const [cid, prods] of owned) {
    for (const fam of PRODUCT_FAMILIES) {
      const has = fam.products.filter((p) => prods.has(p));
      if (!has.length) continue;
      for (const missing of fam.products) {
        if (prods.has(missing)) continue;
        out.push({
          kind: "product_gap_cross_sell",
          customerId: cid,
          customerName: names.get(cid) ?? null,
          fromProduct: has[0],
          toProduct: missing,
          reason: `已购「${productName(has[0])}」，${fam.name}互补品「${productName(missing)}」未购`,
          signalValue: 50,
          evidence: { family: fam.name, ownedInFamily: has, missingProduct: missing },
        });
      }
    }
  }
  return out;
}

// ── 规则 3：expiring_renewal ────────────────────────────────────────
/** N 天内到期的授权（复用 ledger listLicenses 的 expiringInDays 口径，并按 getStats
 *  同款排除 revoked/expired）。未归属客户的到期授权不进清单（授权台账仍可见，
 *  归属后自动出现）。信号值随紧迫度上升：70 + (N − 剩余天数)，封顶 95。 */
export function expiringRenewalOpportunities(
  db: Database.Database = getLedgerDb(),
  days = 30
): Opportunity[] {
  const { rows } = listLicenses({ expiringInDays: days, limit: 500 }, db);
  if (!rows.length) return [];
  const names = customerNameMap(db);
  const out: Opportunity[] = [];
  for (const l of rows) {
    if (!l.customer_id) continue;
    if (l.status && ["revoked", "expired"].includes(l.status)) continue;
    const t = Date.parse(l.expires_at ?? "");
    const daysLeft = Number.isFinite(t) ? Math.max(0, Math.ceil((t - Date.now()) / 86400000)) : days;
    const product = l.product_id ?? l.source_system;
    out.push({
      kind: "expiring_renewal",
      customerId: l.customer_id,
      customerName: names.get(l.customer_id) ?? null,
      fromProduct: product,
      toProduct: product,
      reason: `授权 ${l.source_key} 将于 ${daysLeft} 天后到期，建议续费`,
      signalValue: Math.min(95, 70 + Math.max(0, days - daysLeft)),
      evidence: {
        licenseId: l.id,
        sourceKey: l.source_key,
        sourceSystem: l.source_system,
        expiresAt: l.expires_at ?? "",
        daysLeft,
      },
    });
  }
  return out;
}

// ── 汇总 / 查询 / 统计 ──────────────────────────────────────────────
function safeRule(fn: () => Opportunity[]): Opportunity[] {
  try {
    return fn();
  } catch {
    // 表不存在 / 库损坏 → 该类商机按空处理，不影响其余规则
    return [];
  }
}

function collect(db: Database.Database, kinds: readonly OpportunityKind[]): Opportunity[] {
  const out: Opportunity[] = [];
  if (kinds.includes("persona_cross_sell")) out.push(...safeRule(() => personaCrossSellOpportunities(db)));
  if (kinds.includes("product_gap_cross_sell")) out.push(...safeRule(() => productGapOpportunities(db)));
  if (kinds.includes("expiring_renewal")) out.push(...safeRule(() => expiringRenewalOpportunities(db)));
  return out;
}

const KIND_ORDER: Record<OpportunityKind, number> = {
  persona_cross_sell: 0,
  expiring_renewal: 1,
  product_gap_cross_sell: 2,
};

export interface OpportunityFilter {
  /** 三类之一；未知值返回空数组（API 层另有 400 校验）。 */
  kind?: string;
  customerId?: string;
  /** 默认 100，上限 500。 */
  limit?: number;
}

/** 商机清单：signalValue 降序（同分按类型/客户稳定排序）。只读，任何失败返回 []。 */
export function listOpportunities(
  filter: OpportunityFilter = {},
  db?: Database.Database
): Opportunity[] {
  let rows: Opportunity[];
  try {
    const kind = filter.kind?.trim();
    let kinds: readonly OpportunityKind[] = OPPORTUNITY_KINDS;
    if (kind) {
      if (!isOpportunityKind(kind)) return [];
      kinds = [kind];
    }
    rows = collect(db ?? getLedgerDb(), kinds);
  } catch {
    return [];
  }
  const customerId = filter.customerId?.trim();
  if (customerId) rows = rows.filter((o) => o.customerId === customerId);
  rows.sort(
    (a, b) =>
      b.signalValue - a.signalValue ||
      KIND_ORDER[a.kind] - KIND_ORDER[b.kind] ||
      a.customerId.localeCompare(b.customerId) ||
      a.toProduct.localeCompare(b.toProduct)
  );
  const limit = Math.min(Math.max(1, Math.trunc(filter.limit ?? 100)), 500);
  return rows.slice(0, limit);
}

export interface OpportunityStats {
  total: number;
  byKind: Record<OpportunityKind, number>;
  generatedAt: string;
}

/** 各类商机计数（不设上限，全量重算）。 */
export function getOpportunityStats(db?: Database.Database): OpportunityStats {
  const byKind: Record<OpportunityKind, number> = {
    persona_cross_sell: 0,
    product_gap_cross_sell: 0,
    expiring_renewal: 0,
  };
  let rows: Opportunity[] = [];
  try {
    rows = collect(db ?? getLedgerDb(), OPPORTUNITY_KINDS);
  } catch {
    // 账本不可用 → 全零
  }
  for (const o of rows) byKind[o.kind]++;
  return { total: rows.length, byKind, generatedAt: new Date().toISOString() };
}
