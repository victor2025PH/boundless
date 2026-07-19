// /api/console/leads —— 留资列表（GET ?status=&q=&test=1）与归属客户（PATCH）。
// ?test=1 → includeTest：把测试/演练数据（is_test=1）一并带出，默认排除。
// 日常跟进仍在 /admin（lead-store JSON 为主真相源），本接口只读账本镜像 + 客户归并。
// PATCH body: { source_key, customer_id }，写 audit（actor=console）。
import { NextRequest, NextResponse } from "next/server";
import { requireConsole } from "@/lib/console-auth";
import { listLeads } from "@/lib/ledger";
import { handleAssignPatch } from "../assign";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const sp = req.nextUrl.searchParams;
    const result = listLeads({
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
  return handleAssignPatch(req, "lead");
}
