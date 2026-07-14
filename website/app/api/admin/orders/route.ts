import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import { listOrders } from "@/lib/order-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 管理员/履约机拉订单列表（可按状态过滤）。含联系方式与指纹——仅鉴权后返回，供本地签发履约用。
export async function GET(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const status = req.nextUrl.searchParams.get("status") || "";
  const orders = await listOrders(status || undefined);
  return NextResponse.json({ ok: true, count: orders.length, orders });
}
