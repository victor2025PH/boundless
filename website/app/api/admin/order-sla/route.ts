import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import { runOrderSla } from "@/lib/order-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 订单 SLA 巡检（服务器 cron 每 10 分钟带 key 调用）：
//   ① 已到账超时未开通 → 告警管理员（履约机疑似离线，带一键开通）
//   ② 已开通订阅临期 → 私信客户续费 + 提醒管理员
// 幂等：命中项落去重标记，重复调用不重复打扰。
async function handle(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const r = await runOrderSla();
  return NextResponse.json({ ok: true, ...r });
}

export const GET = handle;
export const POST = handle;
