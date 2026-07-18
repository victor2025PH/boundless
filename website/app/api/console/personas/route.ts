// /api/console/personas —— 人设注册表列表（GET ?q=&status=&customer_id=&product_id=）。
// 人设总线（schema v3）：只存元数据与槽位指纹，资产本体在各引擎侧。
// RBAC：GET viewer+。写操作（grant/revoke/purge/assign_customer）在 [id] 路由，admin+。
import { NextRequest, NextResponse } from "next/server";
import { requireConsole } from "@/lib/console-auth";
import { listPersonas } from "@/lib/personas";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const sp = req.nextUrl.searchParams;
    const result = listPersonas({
      q: sp.get("q") ?? undefined,
      status: sp.get("status") ?? undefined,
      customerId: sp.get("customer_id") ?? undefined,
      productId: sp.get("product_id") ?? undefined,
      limit: sp.get("limit") ? Number(sp.get("limit")) : undefined,
      offset: sp.get("offset") ? Number(sp.get("offset")) : undefined,
    });
    return NextResponse.json({ ok: true, ...result });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
