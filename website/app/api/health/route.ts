import { NextRequest, NextResponse } from "next/server";
import { gatherHealth } from "@/lib/health";
import { requireAdmin } from "@/lib/admin-auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  // deep checks (live external calls) are gated behind the admin key
  const deep = req.nextUrl.searchParams.get("deep") === "1" && requireAdmin(req);
  const h = await gatherHealth(deep);
  // caps = 授权服务能力自声明（产品端 license.probe_server_caps 第一优先读它，免逐端点推断）。
  // 本站有 activate/refresh/revocations/telemetry；「试用签发」需私钥在线签、不在本站——
  // 明告 false，产品端「一键试用」按钮对官网客户自动收起（P0-1 能力门控，2026-08-05）。
  const caps = { activate: true, refresh: true, trial_upgrade: false, revocations: true, telemetry: true };
  return NextResponse.json({ ...h, caps }, { status: h.healthy ? 200 : 503 });
}
