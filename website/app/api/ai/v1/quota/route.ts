import { NextRequest, NextResponse } from "next/server";
import { gatewayEnabled, quotaSnapshot, verifyDeviceToken } from "@/lib/ai-gateway";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * GET /api/ai/v1/quota — 设备令牌持有者查询自己的当日额度。
 * 桌面工作台绿条的额度徽章数据源（60s 级轮询，读 JSON 快照零上游成本）。
 */
export async function GET(req: NextRequest) {
  try {
    if (!gatewayEnabled()) {
      return NextResponse.json({ ok: false, error: "gateway_disabled" }, { status: 503 });
    }
    const h = req.headers.get("authorization") || "";
    const m = /^Bearer\s+(.+)$/i.exec(h.trim());
    const claims = m ? verifyDeviceToken(m[1].trim()) : null;
    if (!claims) {
      return NextResponse.json({ ok: false, error: "invalid_token" }, { status: 401 });
    }
    const q = await quotaSnapshot(claims);
    return NextResponse.json({ ok: true, ...q });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
