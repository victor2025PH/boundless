// /api/console/licenses —— 授权台账列表（GET ?source_system=&status=&expiring_days=）与归属客户（PATCH）。
// 数据来源：tools/license_ledger 导出 → scripts/ledger-import-licenses.mjs 导入。
// PATCH body: { id 或 source_key, customer_id }，写 audit（actor=console）。
import { NextRequest, NextResponse } from "next/server";
import { requireConsole } from "@/lib/console-auth";
import { listLicenses } from "@/lib/ledger";
import { handleAssignPatch } from "../assign";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const sp = req.nextUrl.searchParams;
    const expiring = sp.get("expiring_days");
    const expiringDays = expiring != null && expiring !== "" ? Number(expiring) : undefined;
    const result = listLicenses({
      sourceSystem: sp.get("source_system") ?? undefined,
      status: sp.get("status") ?? undefined,
      customerId: sp.get("customer_id") ?? undefined,
      expiringInDays: Number.isFinite(expiringDays) ? expiringDays : undefined,
      limit: sp.get("limit") ? Number(sp.get("limit")) : undefined,
      offset: sp.get("offset") ? Number(sp.get("offset")) : undefined,
    });
    return NextResponse.json({ ok: true, ...result });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}

export async function PATCH(req: NextRequest) {
  return handleAssignPatch(req, "license");
}
