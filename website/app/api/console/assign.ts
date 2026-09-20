// 归属客户的共用 PATCH 处理：orders / licenses / leads 三路由复用。
// body: { id? , source_key?, customer_id } —— key 取 id 或 source_key（ledger.assignCustomer
// 对 orders/licenses 两者都认，leads 只有 source_key）。
// customer_id 显式传 null = 解除归属（纠错场景：归错人了先解再绑；缺省仍是必填校验）。
// 归属对已归属行直接改绑（覆盖写 + audit 留痕），与「解绑→再绑」等价但少一次往返。
// RBAC：写操作需 admin+（viewer 403）；audit actor="console:<username>"。
import { NextRequest, NextResponse } from "next/server";
import { getConsoleUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { assignCustomer, getLedgerDb, unassignCustomer, type LedgerEntity } from "@/lib/ledger";

export async function handleAssignPatch(req: NextRequest, entity: LedgerEntity): Promise<NextResponse> {
  const user = getConsoleUser(req);
  if (!user) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!roleAtLeast(user.role, "admin")) {
    return NextResponse.json({ error: "forbidden: admin role required" }, { status: 403 });
  }
  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid json body" }, { status: 400 });
  }
  const key = String(body.id ?? body.source_key ?? "").trim();
  if (!key) {
    return NextResponse.json({ error: "id or source_key required" }, { status: 400 });
  }
  const actor = `console:${user.username}`;
  try {
    if (body.customer_id === null) {
      const ok = unassignCustomer(entity, key, getLedgerDb(), actor);
      if (!ok) {
        return NextResponse.json(
          { error: `unassign failed: ${entity} not found or not assigned` },
          { status: 404 }
        );
      }
      return NextResponse.json({ ok: true, entity, key, customer_id: null });
    }
    const customerId = String(body.customer_id ?? "").trim();
    if (!customerId) {
      return NextResponse.json({ error: "customer_id required" }, { status: 400 });
    }
    const ok = assignCustomer(entity, key, customerId, getLedgerDb(), actor);
    if (!ok) {
      return NextResponse.json(
        { error: `assign failed: ${entity} or customer not found` },
        { status: 404 }
      );
    }
    return NextResponse.json({ ok: true, entity, key, customer_id: customerId });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
