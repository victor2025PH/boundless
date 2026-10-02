import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import { runSupportSla } from "@/lib/chatx-support";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 客服工单超时提醒（手动 / 单独调用；日常由 order-sla 的 10 分钟 cron 顺带执行）。幂等。
async function handle(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  return NextResponse.json({ ok: true, ...(await runSupportSla()) });
}

export const GET = handle;
export const POST = handle;
