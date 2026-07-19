// GET /api/console/personas/purges —— 人设清除队列监控（P5 运营收尾，只读）。
// ?target=&state=(pending|acked)&q=&limit=&offset= → { stats, rows, total, limit, offset }。
// stats：待回执/滞留(>24h/>72h)/回执时延/逐引擎积压。RBAC：viewer+（巡检脚本可走
// x-console-key 头通道）。发起清除在 [id] 路由 POST {action:"purge"}（admin+）；
// 引擎回执只认 /api/sync/personas/purges 机器通道 —— 本路由不提供任何写操作。
// 静态段优先于 [id] 动态段，不会与人设详情 GET 冲突（人设 ID 均为 prs_ 前缀）。
import { NextRequest, NextResponse } from "next/server";
import { requireConsole } from "@/lib/console-auth";
import { getPurgeQueueStats, listPurgeQueue } from "@/lib/personas";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const sp = req.nextUrl.searchParams;
    const result = listPurgeQueue({
      target: sp.get("target") ?? undefined,
      state: sp.get("state") ?? undefined,
      q: sp.get("q") ?? undefined,
      limit: sp.get("limit") ? Number(sp.get("limit")) : undefined,
      offset: sp.get("offset") ? Number(sp.get("offset")) : undefined,
    });
    return NextResponse.json({ ok: true, stats: getPurgeQueueStats(), ...result });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
