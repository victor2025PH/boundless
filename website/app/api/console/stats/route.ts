// GET /api/console/stats —— 集团账本总览统计（getStats 直出）。
import { NextRequest, NextResponse } from "next/server";
import { requireConsole } from "@/lib/console-auth";
import { getStats } from "@/lib/ledger";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    return NextResponse.json({ ok: true, stats: getStats() });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
