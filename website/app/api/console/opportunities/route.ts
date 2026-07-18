// GET /api/console/opportunities —— 跨售商机清单（?kind=&customerId=&limit=）。
// 三类规则见 lib/opportunities.ts（persona_cross_sell / product_gap_cross_sell /
// expiring_renewal）。纯只读派生数据（不落库），viewer+ 可读；「标记已跟进」
// 写操作待 opportunities_log 表（下阶段），本路由暂无 POST。
import { NextRequest, NextResponse } from "next/server";
import { requireConsole } from "@/lib/console-auth";
import {
  OPPORTUNITY_KINDS,
  getOpportunityStats,
  isOpportunityKind,
  listOpportunities,
} from "@/lib/opportunities";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const sp = req.nextUrl.searchParams;
    const kind = sp.get("kind") ?? undefined;
    if (kind && !isOpportunityKind(kind)) {
      return NextResponse.json(
        { error: `unknown kind: ${kind} (expect ${OPPORTUNITY_KINDS.join("|")})` },
        { status: 400 }
      );
    }
    const rows = listOpportunities({
      kind,
      // 与 console 其他路由的 snake_case 习惯兼容：customerId / customer_id 都收
      customerId: sp.get("customerId") ?? sp.get("customer_id") ?? undefined,
      limit: sp.get("limit") ? Number(sp.get("limit")) : undefined,
    });
    return NextResponse.json({ ok: true, rows, total: rows.length, stats: getOpportunityStats() });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
