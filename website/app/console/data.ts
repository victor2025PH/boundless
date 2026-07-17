// /console 专用数据薄封装 —— 仅补 lib/ledger.ts 未导出的只读查询。
//
// 原则：写操作一律走 ledger.ts 导出（createCustomer/attachIdentity/assignCustomer/
// writeAudit），本文件只做 SELECT；ledger.ts 属数据层同事维护，不改其一行。
// 当前缺口（ledger.ts 无对应导出，故在此薄封装）：
//   1. 按 id 取单个客户（ledger 只有 listCustomers 模糊搜索）；
//   2. 按客户列出身份标识 identities；
//   3. 按客户聚合审计流水（客户 360 的"审计提示"分区）。

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
