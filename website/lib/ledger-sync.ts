// 现有 JSON 存储（order-store / lead-store）→ 集团账本（ledger.ts）的同步桥。
//
// 双写钩子入口：syncOrderEntry / syncLeadEntry —— 全程 try/catch 吞错、绝不 throw、
// 绝不影响下单/留资主链路（JSON 仍是主真相源，账本只是影子镜像）。
// 回填入口：backfillFromJson —— 读 orders-db.json / leads-db.json 全量幂等 upsert。
// CLI 版回填见 scripts/ledger-backfill.mjs（纯 JS 复刻，勿忘同步语义变更）。

import { readFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";
import {
  ensureCustomerForLead,
  ensureCustomerForOrder,
  getLedgerDb,
  inferProductId,
  isTestSignal,
  upsertLeadRow,
  upsertOrderRow,
  type LeadRowInput,
  type OrderRowInput,
} from "./ledger";
import { resolveOrderSku } from "./offer-map";
// 仅类型导入：不产生运行时依赖，避免与 order-store/lead-store 的动态 import 成环。
import type { LeadEntry } from "./lead-store";
import type { OrderEntry } from "./order-store";

// ── 结构转换（纯函数，console 也可复用）────────────────────────────
export function orderEntryToRow(o: OrderEntry): OrderRowInput {
  // sku_id/product_id：订单自带（新订单 createOrder 出生即填）则直传；历史订单缺失时
  // 先走 offer-map 静态映射推断，product_id 仍不中再走 inferProductId 关键词宽松推断。
  const resolved = resolveOrderSku(o.plan, o.edition, o.period);
  return {
    source_key: o.id,
    product_id: o.product_id ?? resolved.productId ?? inferProductId(o.plan, o.edition),
    sku_id: o.sku_id ?? resolved.skuId,
    plan: o.plan ?? null,
    edition: o.edition ?? null,
    period: o.period ?? null,
    amount: o.amount ?? null,
    pay_amount: o.pay_amount ?? null,
    currency: o.currency ?? null,
    status: o.status ?? null,
    contact: o.contact ?? null,
    fingerprint: o.fingerprint ?? null,
    lang: o.lang ?? null,
    created_at: o.t ?? null,
    paid_at: o.paid_at ?? null,
    activated_at: o.activated_at ?? null,
    notify_chat: o.notify_chat != null ? String(o.notify_chat) : null,
    code: o.code ?? null,
    raw: JSON.stringify(o),
    // e2e/演练单出生即标（contact 如 e2e-notify@internal、指纹如 E2ENOTIFYFP01）
    is_test: isTestSignal(o.contact, o.fingerprint) ? 1 : 0,
  };
}

export function leadEntryToRow(e: LeadEntry): LeadRowInput {
  return {
    source_key: e.id,
    name: e.name ?? null,
    contact: e.contact ?? null,
    interest: e.interest ?? null,
    message: e.message ?? null,
    lang: e.lang ?? null,
    source: e.source ?? null,
    utm: e.utm ?? null,
    status: e.status ?? null,
    first_seen: e.firstSeen ?? null,
    last_seen: e.lastSeen ?? null,
    count: e.count ?? null,
    raw: JSON.stringify(e),
    // e2e/演练留资出生即标（contact/name/来源键任一命中测试信号）
    is_test: isTestSignal(e.contact, e.name, e.id) ? 1 : 0,
  };
}

// ── best-effort 双写（钩子调用；失败静默）──────────────────────────
/** 订单镜像进账本 + 实时归并（命中已有客户即归属，无主强信号自动建档）。
 *  归并失败只 warn 不阻断入账；整体任何异常吞掉，绝不 throw。 */
export function syncOrderEntry(o: OrderEntry): void {
  try {
    if (!o?.id) return;
    const db = getLedgerDb();
    upsertOrderRow(orderEntryToRow(o), db);
    try {
      ensureCustomerForOrder(o.id, db);
    } catch (err) {
      // fail-safe：归并挂了订单仍已入账，留 warn 便于巡检（回扫脚本可补）
      console.warn(`[ledger-sync] link customer for order ${o.id} failed:`, err);
    }
  } catch {
    /* shadow ledger is best-effort — never break the main flow */
  }
}

/** 留资镜像进账本 + 实时归并（语义同订单）。任何异常吞掉，绝不 throw。 */
export function syncLeadEntry(e: LeadEntry): void {
  try {
    if (!e?.id) return;
    const db = getLedgerDb();
    upsertLeadRow(leadEntryToRow(e), db);
    try {
      ensureCustomerForLead(e.id, db);
    } catch (err) {
      console.warn(`[ledger-sync] link customer for lead ${e.id} failed:`, err);
    }
  } catch {
    /* shadow ledger is best-effort — never break the main flow */
  }
}

// ── 全量回填 ────────────────────────────────────────────────────────
export interface BackfillTableStats {
  file: string;
  found: boolean;
  scanned: number;
  inserted: number;
  updated: number;
  skipped: number;
  errors: number;
}

export interface BackfillStats {
  dbPath: string;
  orders: BackfillTableStats;
  leads: BackfillTableStats;
}

export interface BackfillOptions {
  ordersDbPath?: string;
  leadsDbPath?: string;
  /** 账本 DB 路径，默认 resolveLedgerDbPath()（LEDGER_DB env / DATA_DIR/group-ledger.db）。 */
  dbPath?: string;
}

async function readJsonFile(file: string): Promise<unknown | undefined> {
  try {
    return JSON.parse(await readFile(file, "utf-8"));
  } catch {
    return undefined; // 文件不存在或损坏 → 按"没有该表数据"处理
  }
}

/** 读现有两个 JSON db 全量幂等回填账本。可重复执行；第二遍应 0 新增。
 *  与钩子不同：这里的异常按行计数（errors），文件级问题标记 found=false，函数本身不 throw。 */
export async function backfillFromJson(opts: BackfillOptions = {}): Promise<BackfillStats> {
  const ordersFile = opts.ordersDbPath || process.env.ORDERS_DB || path.join(DATA_DIR, "orders-db.json");
  const leadsFile = opts.leadsDbPath || process.env.LEADS_DB || path.join(DATA_DIR, "leads-db.json");
  const db = getLedgerDb(opts.dbPath);
  const dbPath = String(db.name);

  const orders: BackfillTableStats = { file: ordersFile, found: false, scanned: 0, inserted: 0, updated: 0, skipped: 0, errors: 0 };
  const ordersJson = (await readJsonFile(ordersFile)) as { orders?: Record<string, OrderEntry> } | undefined;
  if (ordersJson?.orders && typeof ordersJson.orders === "object") {
    orders.found = true;
    for (const [key, value] of Object.entries(ordersJson.orders)) {
      orders.scanned++;
      try {
        const entry = { ...value, id: value?.id || key };
        if (!entry.id) {
          orders.skipped++;
          continue;
        }
        const res = upsertOrderRow(orderEntryToRow(entry), db);
        if (res.inserted) orders.inserted++;
        else orders.updated++;
        ensureCustomerForOrder(entry.id, db);
      } catch {
        orders.errors++;
      }
    }
  }

  const leads: BackfillTableStats = { file: leadsFile, found: false, scanned: 0, inserted: 0, updated: 0, skipped: 0, errors: 0 };
  const leadsJson = (await readJsonFile(leadsFile)) as { leads?: Record<string, LeadEntry> } | undefined;
  if (leadsJson?.leads && typeof leadsJson.leads === "object") {
    leads.found = true;
    for (const [key, value] of Object.entries(leadsJson.leads)) {
      leads.scanned++;
      try {
        const entry = { ...value, id: value?.id || key };
        if (!entry.id) {
          leads.skipped++;
          continue;
        }
        const res = upsertLeadRow(leadEntryToRow(entry), db);
        if (res.inserted) leads.inserted++;
        else leads.updated++;
        ensureCustomerForLead(entry.id, db);
      } catch {
        leads.errors++;
      }
    }
  }

  return { dbPath, orders, leads };
}
