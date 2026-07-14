import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import { buildOpsSummary, formatOpsSummary } from "@/lib/ops-summary";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** 运营速览 JSON + TG 格式文本。TG /ops 命令与周报同源，此接口供脚本/看板拉数。 */
export async function GET(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const s = await buildOpsSummary();
  return NextResponse.json({ ok: true, summary: s, text: formatOpsSummary(s) });
}
