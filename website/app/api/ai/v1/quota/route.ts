import { NextRequest, NextResponse } from "next/server";
import { gatewayEnabled, quotaSnapshot, verifyDeviceToken } from "@/lib/ai-gateway";
import { hasPendingDiag } from "@/lib/diag-requests";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * GET /api/ai/v1/quota — 设备令牌持有者查询自己的当日额度。
 * 桌面工作台绿条的额度徽章数据源（60s 级轮询，读 JSON 快照零上游成本）。
 *
 * 实施51（2026-08-21）：响应捎带 diag_requested 位——客服登记过针对本机指纹
 * （claims.mid）的远程取包请求且尚未兑现时为 true，客户端见位即后台触发既有
 * check_remote_diag（GET /api/diag-request 确认 + 打包直传 + ack 销记）。
 * 远程拉取时延从「每小时守护轮」压到「工作台开着 ~1 分钟」；仅在 true 时携带
 * 该键（老客户端零感知），查询是本地小 JSON 读、失败静默不影响额度主链。
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
    let diagRequested = false;
    try {
      diagRequested = await hasPendingDiag(claims.mid);
    } catch {
      /* 诊断捎带位绝不拖垮额度主链 */
    }
    return NextResponse.json({ ok: true, ...q, ...(diagRequested ? { diag_requested: true } : {}) });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
