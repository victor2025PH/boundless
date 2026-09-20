// /console 专用数据薄封装 —— 仅补 lib/ledger.ts 未导出的只读查询。
//
// 原则：写操作一律走 ledger.ts 导出（createCustomer/attachIdentity/assignCustomer/
// writeAudit），本文件只做 SELECT。
// 当前缺口（ledger.ts 无对应导出，故在此薄封装）：
//   1. 按 id 取单个客户（ledger 只有 listCustomers 模糊搜索）；
//   2. 按客户列出身份标识 identities；
//   3. 按客户聚合审计流水（客户 360 的"审计提示"分区）；
//   4. 全局审计列表 listAudit（?q=&action=&days=，独立审计页，带分页）；
//   5. 总览「今日待办」计数 + 「本月成交」收入快照（纯 SELECT 聚合）；
//   6. 试用→付费转化：identities 全量映射 + 成交客户 id 集合（试用页解析领取归属用）。

import { getLedgerDb, type AuditRow, type CustomerRow, type IdentityRow } from "@/lib/ledger";

/** 按主键取客户行；不存在返回 null。 */
export function getCustomerById(id: string): CustomerRow | null {
  const row = getLedgerDb()
    .prepare("SELECT * FROM customers WHERE id = ?")
    .get(id) as CustomerRow | undefined;
  return row ?? null;
}

/** 客户名下全部身份标识（按创建先后）。 */
export function listIdentitiesByCustomer(customerId: string): IdentityRow[] {
  return getLedgerDb()
    .prepare("SELECT * FROM identities WHERE customer_id = ? ORDER BY id ASC")
    .all(customerId) as IdentityRow[];
}

/** 客户相关审计流水：entity='customer' 直连记录 + detail 中引用该客户 id 的归属/自动关联记录。
 *  customer id 为全局唯一 ULID，LIKE 匹配不会误伤。 */
export function listAuditForCustomer(customerId: string, limit = 30): AuditRow[] {
  return getLedgerDb()
    .prepare(
      `SELECT * FROM audit
       WHERE (entity = 'customer' AND entity_id = @id) OR detail LIKE @like
       ORDER BY ts DESC LIMIT @limit`
    )
    .all({ id: customerId, like: `%${customerId}%`, limit }) as AuditRow[];
}

/** 全局审计列表（只读）：支持 action 精确过滤 + q 模糊（actor/action/entity/entity_id/detail）
 *  + days 时间窗（近 N 天）+ offset 分页。不含聊天内容——audit 表只记写操作元数据。 */
export function listAudit(
  opts: { limit?: number; offset?: number; q?: string; action?: string; days?: number } = {}
): {
  rows: AuditRow[];
  total: number;
} {
  const limit = Math.min(Math.max(1, Math.trunc(opts.limit ?? 100)), 500);
  const offset = Math.max(0, Math.trunc(opts.offset ?? 0));
  const action = opts.action?.trim() || undefined;
  const q = opts.q?.trim() || undefined;
  const db = getLedgerDb();

  const where: string[] = [];
  const params: Record<string, string | number> = { limit, offset };
  if (action) {
    where.push("action = @action");
    params.action = action;
  }
  if (q) {
    where.push(
      "(actor LIKE @like OR action LIKE @like OR entity LIKE @like OR entity_id LIKE @like OR IFNULL(detail,'') LIKE @like)"
    );
    params.like = `%${q}%`;
  }
  if (opts.days && Number.isFinite(opts.days) && opts.days > 0) {
    where.push("ts >= @since");
    params.since = new Date(Date.now() - opts.days * 86400_000).toISOString();
  }
  const clause = where.length ? `WHERE ${where.join(" AND ")}` : "";
  const total = (
    db.prepare(`SELECT COUNT(*) AS n FROM audit ${clause}`).get(params) as { n: number }
  ).n;
  const rows = db
    .prepare(`SELECT * FROM audit ${clause} ORDER BY ts DESC LIMIT @limit OFFSET @offset`)
    .all(params) as AuditRow[];
  return { rows, total };
}

// ── 总览「今日待办」──────────────────────────────────────────────────

/** 已成交（paid/activated）但未归属客户的订单数（排除测试数据）。
 *  成交了却没归到客户主档 = 客户 360 与商机引擎都看不见这笔钱，是归并第一优先级。 */
export function countUnassignedPaidOrders(): number {
  try {
    return (
      getLedgerDb()
        .prepare(
          `SELECT COUNT(*) AS n FROM orders
           WHERE customer_id IS NULL AND status IN ('paid','activated') AND COALESCE(is_test,0) = 0`
        )
        .get() as { n: number }
    ).n;
  } catch {
    return 0;
  }
}

// ── 总览「本月成交」收入快照 ─────────────────────────────────────────

export interface RevenueLine {
  product_id: string | null;
  currency: string | null;
  orders: number;
  amount: number;
}

export interface RevenueSnapshot {
  /** 本月起点（站点时区）对应的 UTC ISO。 */
  monthStartIso: string;
  current: RevenueLine[];
  previous: RevenueLine[];
}

const TZ_OFFSET_H = Number(process.env.TZ_OFFSET ?? 8);

/** 站点时区的「本月 / 上月」月初对应的 UTC 毫秒。 */
function monthStartsUtc(): { cur: number; prev: number } {
  const local = new Date(Date.now() + TZ_OFFSET_H * 3600_000);
  const y = local.getUTCFullYear();
  const m = local.getUTCMonth();
  const toUtc = (yy: number, mm: number) => Date.UTC(yy, mm, 1) - TZ_OFFSET_H * 3600_000;
  return { cur: toUtc(y, m), prev: toUtc(y, m - 1) };
}

/** 本月 + 上月成交（paid/activated，排除测试）按产品 × 币种聚合。
 *  记账时点取 COALESCE(paid_at, created_at)——挂牌 USD、结算多为 USDT，币种原样分组不折算。 */
export function revenueSnapshot(): RevenueSnapshot {
  const { cur, prev } = monthStartsUtc();
  const curIso = new Date(cur).toISOString();
  const prevIso = new Date(prev).toISOString();
  const db = getLedgerDb();
  const query = (fromIso: string, toIso: string | null): RevenueLine[] => {
    try {
      return db
        .prepare(
          `SELECT product_id, currency, COUNT(*) AS orders,
                  ROUND(SUM(COALESCE(pay_amount, amount, 0)), 2) AS amount
           FROM orders
           WHERE status IN ('paid','activated') AND COALESCE(is_test,0) = 0
             AND COALESCE(paid_at, created_at) >= @from
             ${toIso ? "AND COALESCE(paid_at, created_at) < @to" : ""}
           GROUP BY product_id, currency
           ORDER BY amount DESC`
        )
        .all(toIso ? { from: fromIso, to: toIso } : { from: fromIso }) as RevenueLine[];
    } catch {
      return [];
    }
  };
  return {
    monthStartIso: curIso,
    current: query(curIso, null),
    previous: query(prevIso, curIso),
  };
}

// ── 试用 → 付费转化 ─────────────────────────────────────────────────

/** identities 全量映射：`kind|value` → customer_id（value 已按 ledger 归一化存储）。
 *  试用台账把领取记录的联系方式归一成同款键后查此映射，即把领取解析到客户主档；
 *  量级 = 身份标识总数（千以内），整取无压力。 */
export function identityCustomerMap(): Map<string, string> {
  try {
    const rows = getLedgerDb()
      .prepare("SELECT kind, value, customer_id AS cid FROM identities")
      .all() as { kind: string; value: string; cid: string }[];
    return new Map(rows.map((r) => [`${r.kind}|${r.value}`, r.cid]));
  } catch {
    return new Map();
  }
}

/** 有成交订单（paid/activated，排除测试）的客户 id 集合——与 identityCustomerMap
 *  合用即得「试用→付费」转化判定。 */
export function paidCustomerIdSet(): Set<string> {
  try {
    const rows = getLedgerDb()
      .prepare(
        `SELECT DISTINCT customer_id AS cid FROM orders
         WHERE customer_id IS NOT NULL AND status IN ('paid','activated') AND COALESCE(is_test,0) = 0`
      )
      .all() as { cid: string }[];
    return new Set(rows.map((r) => r.cid));
  } catch {
    return new Set();
  }
}
