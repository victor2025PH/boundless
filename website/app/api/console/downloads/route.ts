// GET /api/console/downloads?days=7&all=1&product=chatx —— 安装包下载台账（IP / 归属地 / 走向）。
// 口径见 lib/download-ledger.ts 模块头；归属地由 lib/ip-geo.ts 惰性补齐（失败则该列为空）。
import { NextRequest, NextResponse } from "next/server";
import { requireConsole } from "@/lib/console-auth";
import { readLedger } from "@/lib/download-ledger";
import { geoLookup } from "@/lib/ip-geo";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const sp = req.nextUrl.searchParams;
    const days = Math.min(365, Math.max(1, Number(sp.get("days") || 7) || 7));
    const includeNoise = ["1", "true"].includes(sp.get("all") || "");
    const product = sp.get("product") || undefined;
    const limit = Math.min(2000, Math.max(1, Number(sp.get("limit") || 300) || 300));
    const s = await readLedger({ days, includeNoise, product, limit });
    const geo = sp.get("geo") === "0" ? {} : await geoLookup(s.rows.map((r) => r.ip));
    return NextResponse.json({ ok: true, ...s, geo });
  } catch (e) {
    return NextResponse.json(
      { ok: false, error: String((e as Error)?.message || e).slice(0, 200) },
      { status: 500 },
    );
  }
}
