// /api/console/orders —— 订单台账列表（GET ?status=&q=&limit=&offset=&test=1）与归属客户（PATCH）。
// ?test=1 → includeTest：把测试/演练数据（is_test=1）一并带出，默认排除。
// PATCH body: { id 或 source_key, customer_id }，写 audit（actor=console）。
import { NextRequest, NextResponse } from "next/server";
import { requireConsole } from "@/lib/console-auth";
import { listOrders } from "@/lib/ledger";
import { handleAssignPatch } from "../assign";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const sp = req.nextUrl.searchParams;
    const result = listOrders({
      status: sp.get("status") ?? undefined,
      q: sp.get("q") ?? undefined,
      customerId: sp.get("customer_id") ?? undefined,
      limit: sp.get("limit") ? Number(sp.get("limit")) : undefined,
      offset: sp.get("offset") ? Number(sp.get("offset")) : undefined,
      includeTest: sp.get("test") === "1",
    });
    return NextResponse.json({ ok: true, ...result });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}

export async function PATCH(req: NextRequest) {
  return handleAssignPatch(req, "order");
}
