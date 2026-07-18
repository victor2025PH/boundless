// 归属客户的共用 PATCH 处理：orders / licenses / leads 三路由复用。
// body: { id? , source_key?, customer_id } —— key 取 id 或 source_key（ledger.assignCustomer
// 对 orders/licenses 两者都认，leads 只有 source_key）。
// RBAC：写操作需 admin+（viewer 403）；audit actor="console:<username>"。
import { NextRequest, NextResponse } from "next/server";
import { getConsoleUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { assignCustomer, getLedgerDb, type LedgerEntity } from "@/lib/ledger";

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
  const customerId = String(body.customer_id ?? "").trim();
  if (!key) {
    return NextResponse.json({ error: "id or source_key required" }, { status: 400 });
  }
  if (!customerId) {
    return NextResponse.json({ error: "customer_id required" }, { status: 400 });
  }
  try {
    const ok = assignCustomer(entity, key, customerId, getLedgerDb(), `console:${user.username}`);
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
